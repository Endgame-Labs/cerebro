"""Source files: `sources/<name>.yaml`, one per source, read into a typed record.

A source file names its kind, where it points, its window, what its records extract into, and the lines each kind
reads. The file's name without `.yaml` is the source's name. A file that does not read is refused alone, with one
line that names the file, the key and what to do, and the other files still load. A known kind the caller gives
no connector for loads and lists nothing, with a notice, so a workspace that names one still runs.

The keys each kind reads come from its connector's module, through `connectors.KINDS`. `load_sources` takes the kinds
its caller knows, each a builder that turns a source file and the workspace's folder into a connector.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from .agent import read_yaml
from .connector import Connector
from .errors import CerebroError

__all__ = [
    "Build",
    "Select",
    "SourceError",
    "SourceFile",
    "Sources",
    "Window",
    "load_sources",
    "read_source",
    "source_of",
    "span",
]

EXTRACTS = ("facts", "directives", "documents", "records", "none")
ALL = "all"
DEFAULT_BEHIND = "90d"
# The keys the engine reads for a source of every kind. Each kind's own keys, and its reason for refusing a key where
# it gives one, are declared beside the code that reads them, in its connector's module.
ENGINE_KEYS = ("kind",)
# The keys read into a field of their own: every other key a kind reads stays in `settings`, as written.
TYPED = ("kind", "url", "command", "env", "auth", "extract", "freshness", "capabilities", "select", "owner", "window",
         "into", "full_every")
TEMPLATED = ("id", "url", "into", "at")

_DURATION = re.compile(r"^([0-9]+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLLECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_OWNER = re.compile(r"^(people/[a-z0-9][a-z0-9-]*|[^@\s/]+@[^@\s/]+\.[^@\s/]+)$")
_EXTRACT_CHOICES = ", ".join(EXTRACTS[:-1]) + " or " + EXTRACTS[-1]
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,23}$")  # a key a line names; any other is not printed, as it may be a secret
NOT_A_WORD = "a key that is not a word"


class SourceError(CerebroError, ValueError):
    """A source file does not read. Its message is one line that names the file, the key and the fix."""


def span(text: str) -> timedelta:
    """A duration as a source file writes one, such as `90d`, `36h`, `15m` or `30s`."""
    match = _DURATION.match(str(text))
    if match is None or int(match.group(1)) == 0:
        raise ValueError(f"{text!r} is not a duration: write a whole number above zero and a unit, s, m, h or d, "
                         f"as 90d")
    try:
        return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])
    except OverflowError:
        raise ValueError(f"{text!r} is longer than a duration can be: write a shorter one, as 90d") from None


@dataclass
class Window:
    """How far a source reads on either side of now. `behind` is a duration or `all`, and empty means the kind's
    default. `ahead` is read by the calendar alone."""

    behind: str = ""
    ahead: str = ""


@dataclass
class Select:
    """One entry under `select:`: the files a glob matches and how they become records. A relative glob is under
    the workspace's folder and `~/` is the home folder. `extract` overrides the file's own. The templates fill
    `{path}` and `{line}`, and `{row.<column>}` when `rows` reads a file as rows. A source of a kind that selects
    folders reads `select: { folders: [...] }` as one entry that holds `folders` alone."""

    glob: str = ""
    extract: str = ""
    rows: str = ""
    id: str = ""
    url: str = ""
    into: str = ""
    identity: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    at: str = ""
    folders: list[str] = field(default_factory=list)


@dataclass
class SourceFile:
    """One source file, read. `name` is its file's name without `.yaml`. `auth` is a word such as `oauth`, or
    `keychain: <item>`, and never a secret. `settings` holds the lines one kind reads itself, as written."""

    name: str
    kind: str
    path: Path | None = None
    extract: str = ""
    window: Window = field(default_factory=Window)
    select: list[Select] = field(default_factory=list)
    auth: str = ""
    url: str = ""
    command: str = ""
    env: list[str] = field(default_factory=list)
    freshness: str = ""
    full_every: str = ""  # how often a source whose cursor lists only what changed is listed in full: 24h when empty,
    # and at every check when always
    capabilities: list[str] = field(default_factory=list)
    owner: str = ""
    into: str = ""
    settings: dict[str, Any] = field(default_factory=dict)

    def behind_default(self) -> str:
        """How far back the window reaches when the file says nothing: all of it
        for a files source, since what you drop into a folder you mean to read, and for an mcp source whose
        binding.list args never name {since}, which cannot ask for less; 90 days for every other source."""
        binding = self.settings.get("binding") if self.kind == "mcp" else None
        listing = binding.get("list") if isinstance(binding, dict) else None
        whole = isinstance(listing, dict) and "{since}" not in json.dumps(listing.get("args"), default=str)
        return ALL if self.kind == "files" or whole else DEFAULT_BEHIND

    def behind(self) -> timedelta | None:
        """How far back the window reaches, or None for all of it. A files source reads all of it by default,
        since what you drop into a folder you mean to read, and every other kind reads 90 days."""
        behind = self.window.behind or (ALL if self.kind == "files" else DEFAULT_BEHIND)
        return None if behind == ALL else span(behind)


Build = Callable[[SourceFile, Path], Connector]


@dataclass
class Sources:
    """What a workspace's `sources/` folder gave: a connector for each source of a kind the caller knows, every file
    that read, a line for each file refused, and a notice for each kind given no connector."""

    connectors: list[Connector] = field(default_factory=list)
    files: list[SourceFile] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


class _Refuse(Exception):
    """One problem with one key, before the file's name is put in front of it. `at` is the key's place in the file,
    as ("select", 0, "rows"), when the line it stands on is to be named. `also` holds the file's other key problems,
    so one line names them all."""

    def __init__(self, message: str, at: tuple[str | int, ...] = (), also: list[_Refuse] | None = None):
        super().__init__(message)
        self.at = at
        self.also = also or []


def shown_key(key: Any) -> str:
    """A key's name as a refusal prints it: a short word as written, and anything else never, since a token or a
    connection string pasted where a key goes would be printed with it."""
    return str(key) if _WORD.fullmatch(str(key)) else NOT_A_WORD


def _text(value: Any, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _Refuse(f"{key}: write one value, not a list or a mapping")
    return str(value)


def _texts(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Refuse(f"{key}: write a list, as {key}: [a, b]")
    return [_text(item, f"{key}[{i}]") for i, item in enumerate(value)]


def _mapping(value: Any, key: str, example: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _Refuse(f"{key}: write a mapping, as {example}")
    return {str(k): v for k, v in value.items()}


def _unknown(block: dict[str, Any], known: tuple[str, ...], where: str, at: tuple[str | int, ...] = ()) -> None:
    for key in block:
        if key not in known:
            raise _Refuse(f"{where}{shown_key(key)} is not a key a source file reads: delete it, or fix its spelling "
                          f"({', '.join(known)})", (*at, key))


def _kinds() -> dict[str, Any]:
    from .connectors import KINDS  # the connectors import this module

    return KINDS


def _top(key: str, kind: str, known: tuple[str, ...]) -> str:
    """Why a top-level key a source of `kind` does not read is refused: its connector's reason, the kinds that do read
    it, or that it is no key at all."""
    kinds, shown = _kinds(), shown_key(key)
    if key in getattr(kinds[kind], "REFUSED_KEYS", {}):
        return f"{key}: {kinds[kind].REFUSED_KEYS[key]}"
    others = sorted(name for name, module in kinds.items() if key in module.SOURCE_KEYS)
    if others:
        return f"{key} belongs to a source of kind {_either(others)}: delete it"
    if shown == NOT_A_WORD:
        return f"{NOT_A_WORD} is not one a source of kind {kind} reads: delete it"
    return f"{key} is not a key a source of kind {kind} reads: delete it, or fix its spelling ({', '.join(known)})"


def _faults(data: dict[str, Any], kind: str) -> list[_Refuse]:
    """Every key of the file a source of `kind` does not read, at the top, in a select entry or in a mapping its
    connector declares the keys of, each with the reason it gives, in the order of the file."""
    own = _kinds()[kind]
    known = ENGINE_KEYS + own.SOURCE_KEYS
    found = [_Refuse(_top(key, kind, known), (key,)) for key in data if key not in known]
    if "window" in known and isinstance(data.get("window"), dict):
        found += [_Refuse(f"window.{shown_key(key)} is not a key a source file reads: delete it, or fix its spelling "
                          f"(behind, ahead)", ("window", key)) for key in data["window"] if key not in ("behind", "ahead")]
    reads = getattr(own, "SELECT_KEYS", ())
    if "select" in known and "glob" in reads and isinstance(data.get("select"), list):
        for i, raw in enumerate(data["select"]):
            for key in raw if isinstance(raw, dict) else ():
                if key not in reads:
                    found.append(_Refuse(
                        f"select[{i}].{shown_key(key)} is not a key a select entry of a source of kind {kind} reads: "
                        f"delete it, or fix its spelling ({', '.join(reads)})",
                        ("select", i, key)))
    for path, keys in getattr(own, "NESTED_KEYS", {}).items():
        blocks: list[tuple[tuple[str, ...], Any]] = [((), data)]
        for step in path:
            blocks = [((*at, str(k)), v) for at, block in blocks if isinstance(block, dict)
                      for k, v in block.items() if step in ("*", k)]
        for at, block in blocks:
            for key in block if isinstance(block, dict) else ():
                if key not in keys:
                    where = ".".join(shown_key(step) for step in (*at, key))
                    found.append(_Refuse(f"{where} is not a key a source of kind {kind} reads: delete it, or fix its "
                                         f"spelling ({', '.join(keys)})", (*at, str(key))))
    return found


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _duration(value: str, key: str, *, all_too: bool = False) -> None:
    if all_too and value == ALL:
        return
    try:
        span(value)
    except ValueError as error:
        raise _Refuse(f"{key}: {error}{', or all' if all_too else ''}") from None


def _templates(entry: Select, where: str) -> None:
    named = [(key, getattr(entry, key)) for key in TEMPLATED]
    named += [(f"identity.{shown_key(k)}", v) for k, v in entry.identity.items()]
    named += [(f"fields.{shown_key(k)}", v) for k, v in entry.fields.items()]
    for key, template in named:
        for name in _PLACEHOLDER.findall(template):
            if name in ("path", "line"):
                continue
            if name.startswith("row.") and len(name) > 4:
                if not entry.rows:
                    raise _Refuse(f"{where}{key}: {{{name}}} needs rows: csv, jsonl or json on the entry")
                continue
            raise _Refuse(f"{where}{key}: {{{name}}} is not a template a source fills: use {{path}}, {{line}} or "
                          f"{{row.<column>}}")


def _select(value: Any, kind: str) -> list[Select]:
    if value is None:
        return []
    reads = _kinds()[kind].SELECT_KEYS
    if isinstance(value, dict) or "glob" not in reads:
        if "folders" not in reads or not isinstance(value, dict) or set(value) != {"folders"}:
            if "folders" in reads:
                raise _Refuse(f"select: a {kind} source reads folders alone, never a glob: write "
                              f"select: {{ folders: [Projects] }}", ("select",))
            raise _Refuse(f"select: a source of kind {kind} takes select: [{{ glob: \"notes/**/*.md\" }}]",
                          ("select",))
        return [Select(folders=_texts(value["folders"], "select.folders"))]
    if not isinstance(value, list):
        raise _Refuse("select: write a list of entries, each with a glob, as select: [{ glob: \"notes/**/*.md\" }]")
    entries = []
    for i, raw in enumerate(value):
        where = f"select[{i}]."
        block = _mapping(raw, f"select[{i}]", "{ glob: \"notes/**/*.md\" }")
        entry = Select(**{key: _text(block.get(key), where + key) for key in reads
                          if key not in ("identity", "fields")})
        entry.identity = {k: _text(v, f"{where}identity.{shown_key(k)}")
                          for k, v in _mapping(block.get("identity"), where + "identity", "{ email: \"x\" }").items()}
        entry.fields = {k: _text(v, f"{where}fields.{shown_key(k)}")
                        for k, v in _mapping(block.get("fields"), where + "fields", "{ title: \"x\" }").items()}
        if not entry.glob.strip():
            raise _Refuse(f"select[{i}]: glob is required: name the files, as glob: \"notes/**/*.md\"")
        if entry.extract and entry.extract not in EXTRACTS:
            raise _Refuse(f"{where}extract {entry.extract}: write one of {_EXTRACT_CHOICES}")
        if entry.rows not in ("", "csv", "jsonl", "json"):
            raise _Refuse(f"{where}rows {entry.rows}: write csv, jsonl or json, or delete it for one record a file")
        _templates(entry, where)
        entries.append(entry)
    return entries


def _window(value: Any, kind: str) -> Window:
    block = _mapping(value, "window", "{ behind: 90d }")
    _unknown(block, ("behind", "ahead"), "window.", ("window",))
    window = Window(_text(block.get("behind"), "window.behind"), _text(block.get("ahead"), "window.ahead"))
    if window.behind:
        _duration(window.behind, "window.behind", all_too=True)
    if window.ahead and kind != "calendar":
        raise _Refuse(f"window.ahead: {kind} reads nothing ahead of now: delete ahead: and keep behind: alone, as "
                      f"window: {{ behind: 90d }}")
    if window.ahead:
        _duration(window.ahead, "window.ahead")
    return window


def _auth(value: Any) -> str:
    if isinstance(value, dict):
        if set(value) != {"keychain"} or not str(value["keychain"] or "").strip():
            raise _Refuse("auth: write a word such as oauth, or { keychain: <item> }")
        return f"keychain: {value['keychain']}"
    return _text(value, "auth")


def _parse(name: str, data: Any) -> SourceFile:
    if not isinstance(data, dict):
        raise _Refuse("the file is not a mapping: start it with kind:, as kind: files")
    data = {str(k): v for k, v in data.items()}
    kind = _text(data.get("kind"), "kind")
    kinds = sorted(_kinds())
    if not kind:
        raise _Refuse(f"kind is missing: name one, such as kind: files ({', '.join(kinds)})")
    if kind not in kinds:
        raise _Refuse(f"kind {kind} is not a kind of source: write one of {', '.join(kinds)}")
    if faults := _faults(data, kind):
        raise _Refuse(str(faults[0]), faults[0].at, faults[1:])
    source = SourceFile(name=name, kind=kind)
    for key in ("url", "command", "freshness", "full_every", "owner", "into", "extract"):
        setattr(source, key, _text(data.get(key), key))
    source.auth = _auth(data.get("auth"))
    source.env = _texts(data.get("env"), "env")
    source.capabilities = _texts(data.get("capabilities"), "capabilities")
    source.window = _window(data.get("window"), kind)
    source.select = _select(data.get("select"), kind)
    source.settings = {key: value for key, value in data.items() if key not in TYPED}
    if source.extract and source.extract not in EXTRACTS:
        raise _Refuse(f"extract {source.extract}: write one of {_EXTRACT_CHOICES}")
    if source.url and source.command:
        raise _Refuse("url and command: a source reaches its server one way: keep one of them")
    if source.env and not source.command:
        raise _Refuse("env names the variables a server started by command takes: add command, or delete env")
    for i, variable in enumerate(source.env):
        if not _NAME.match(variable):
            raise _Refuse(f"env[{i}]: write a variable's name alone, such as NOTES_TOKEN, with its value kept "
                          f"elsewhere")
    if source.freshness:
        _duration(source.freshness, "freshness")
    if source.full_every and source.full_every != "always":  # always: a listing in full at every check
        try:
            span(source.full_every)
        except ValueError as error:
            raise _Refuse(f"full_every: {error}, or always to list in full at every check") from None
    if source.into and not _COLLECTION.match(source.into):
        raise _Refuse(f"into {source.into}: write one folder name under articles/")
    if source.owner and source.owner != "me" and not _OWNER.match(source.owner):
        raise _Refuse(f"owner {source.owner}: write me, or a colleague's email address")
    return source


def read_source(path: Path) -> SourceFile:
    """Read one source file. Raises `SourceError` with one line that names the file, the key and the fix."""
    label = f"sources/{path.name}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SourceError(f"{label} does not read ({type(error).__name__}): save it as UTF-8 text") from None
    source = source_of(path.stem, text, label)
    source.path = path
    return source


def source_of(name: str, text: str, label: str = "") -> SourceFile:
    """The source `name` a file's `text` describes, as `read_source` reads it, for one not written yet. Raises
    `SourceError` as it does."""
    label = label or f"sources/{name}.yaml"
    data, problem = read_yaml(text)
    if problem is not None:
        raise SourceError(f"{label} {problem}: fix the YAML")
    try:
        _words(text, data)
        return _parse(name, data)
    except _Refuse as error:
        said = []
        for fault in (error, *error.also):
            line = _line(text, fault.at)
            said.append(f"{f'line {line}: ' if line else ''}{fault}")
        raise SourceError(f"{label} {'; '.join(said)}") from None


def _words(text: str, data: Any) -> None:
    """Refuse a top-level key YAML reads as something other than text, such as `yes:` or `~:`, as written."""
    if not isinstance(data, dict) or all(isinstance(key, str) for key in data):
        return
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    key = next(k for k, _ in node.value if k.tag != "tag:yaml.org,2002:str")
    raise _Refuse(f"{shown_key(key.value)} is not a key a source file reads: a key is a word, so delete it or fix its "
                  f"spelling", (key.value,))


def _line(text: str, at: tuple[str | int, ...]) -> int | None:
    """The line the key at `at` stands on in a source file's text, counted from 1, or None when it is not found."""
    if not at:
        return None
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    for n, step in enumerate(at, 1):
        if isinstance(node, yaml.MappingNode):
            found = [(key, value) for key, value in node.value if key.value == step]
            if not found:
                return None
            key, node = found[0]
            if n == len(at):
                return key.start_mark.line + 1
        elif isinstance(node, yaml.SequenceNode) and isinstance(step, int) and step < len(node.value):
            node = node.value[step]
        else:
            return None
    return None


def load_sources(root: Path, kinds: Mapping[str, Build]) -> Sources:
    """Turn the `sources/` folder of the workspace at `root` into connectors, one for each source of a kind in `kinds`.

    A file that does not read, or whose builder refuses it, is refused alone. A source of a documented kind not in
    `kinds` loads as known and gets no connector, with a notice. A folder with no `sources/` has no sources.
    """
    found = Sources()
    folder = root / "sources"
    paths = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    for path in paths:
        if path.name.startswith("."):
            continue
        try:
            source = read_source(path)
            found.files.append(source)
            build = kinds.get(source.kind)
            if build is None:
                found.notices.append(f"sources/{path.name}: kind {source.kind} has no connector here: it loads "
                                     f"and lists nothing")
                continue
            found.connectors.append(build(source, root))
        except CerebroError as error:
            found.refused.append(str(error))
    return found
