"""Context agent files: read them, check them, and say what each one is.

A context agent is a markdown file under `agents/`: front matter that configures it, then a prompt. It is never
defined in code. Its name and folder give its role, so a file holds no `id:` and no `target:` its path gives it:

- `<collection>/curator.md` curates the collection, with every curator tool and no `tools:` line.
- `<collection>/article.md` writes each article of the collection. With `subagents:` it composes each article from the
  drafts of the files it lists under `<collection>/subagents/`.
- `<collection>/subagents/<name>.md` drafts one heading of an article its composer composes: its name, as `work-streams`
  writes Work streams, unless it says `heading:`.
- any other `<collection>/<name>.md` writes the one article at its path, or, with `subagents:`, composes it. One
  whose `target:` is a query writes each article of one collection the query matches.

A file that does not load is refused with one line that names the file, the key, and what to write, and every other
file still loads. An evidence option that is read but not acted on is named in a notice.
"""

from __future__ import annotations

import difflib
import hashlib
import inspect
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .apps import bind
from .article import atx
from .errors import CerebroError
from .models import ENTRY_KEYS, ModelSettingsError, read_model
from .store import LOGS, RELATIONS
from .window import is_window, shaped

__all__ = [
    "HEARTBEAT_WORDS", "Agent", "AgentFileError", "Authority", "Budget", "EvidenceLine", "Render",
    "cooldown_seconds", "heading_of", "load_agent", "load_agents", "read_yaml", "refusal_of", "refuse_unknown_kinds",
    "refuse_unknown_tools",
    "refused_ids",
]

KEYS = ("evidence", "heartbeat", "tools", "budget", "model", "subagents", "authority", "rendered", "target",
        "heading")
KEEP_ACTED_ON = ("articles",)  # the lines keep and top act on; on any other they are read and not acted on
HEARTBEAT_WORDS = ("change", "manual", "hourly", "daily", "weekly")
SCHEDULES = {"hourly": "1h", "daily": "1d", "weekly": "7d"}
READ_TOOLS = ("query", "read")
CURATOR_TOOLS = ("admit", "link", "instruct", "archive", "merge", "split", "fetch", "leave_out", "reject")
EVIDENCE_OPTIONS = ("keep", "top", "keep_bar", "summary", "text_only")
FACTS_KEYS = ("classes", "window", "mentions", "latest", "relation")
FILES = ("facts", *LOGS)  # what a query reads as lines, not articles
SITE = ("collections",)
AUTHORITY_KEYS = ("admit", "archive", "merge", "split")
RENDER_KINDS, RENDER_STYLES, RENDER_SHOWS = ("collections",), ("outline", "headings"), ("count", "description")
RENDER_PLACES = ("before", "after")
ID = re.compile(r"[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*")
COOLDOWN = re.compile(r"(\d+(?:\.\d+)?)(h|m|s)")
FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$\n?(.*)\Z", re.DOTALL | re.MULTILINE)
FENCE = re.compile(r"(`{3,}|~{3,})")
BREAKS = re.compile("\r\n|[\r\n\x85\u2028\u2029]")  # what YAML reads as the end of a line


class AgentFileError(CerebroError, ValueError):
    """An agent file does not load. The message is one line: the file, what is wrong, and what to write. `line` is the
    line of the file it names, 0 for none."""

    def __init__(self, message: str, line: int = 0):
        super().__init__(message)
        self.line = line


class _Strict(yaml.SafeLoader):
    """YAML that refuses a key given twice in one map, where plain YAML keeps the last."""


def _no_duplicates(loader: _Strict, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise yaml.constructor.ConstructorError(None, None, "a << merge key is not read here: write each key out",
                                                    key_node.start_mark)
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)) and key is not None:
            raise yaml.constructor.ConstructorError(None, None, "a key is a list or a map: write a plain word",
                                                    key_node.start_mark)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"the key {_key(key)} is given twice",
                                                    key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep)


_Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


class _Written(_Strict):
    """YAML whose map keys are the words as the file writes them, so `on`, `yes`, `1` and `0x10` are each that word."""


def _as_written(loader: _Written, node: yaml.MappingNode) -> dict[str, Any]:
    seen = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise yaml.constructor.ConstructorError(None, None, "a << merge key is not read here: write each key out",
                                                    key_node.start_mark)
        if not isinstance(key_node, yaml.ScalarNode):
            raise yaml.constructor.ConstructorError(None, None, "a key is a list or a map: write a plain word",
                                                    key_node.start_mark)
        if key_node.value in seen:
            raise yaml.constructor.ConstructorError(None, None, f"the key {key_node.value} is given twice",
                                                    key_node.start_mark)
        seen.add(key_node.value)
    return {key_node.value: loader.construct_object(value, deep=True) for key_node, value in node.value}


_Written.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _as_written)


def _key(key: Any) -> str:
    return "on" if key is True else str(key)  # YAML 1.1 reads a bare on: as true


def read_yaml(text: str) -> tuple[Any, str | None]:
    """Parse YAML, refusing duplicate keys. Gives the value, or None and one line that names the line at fault."""
    value, _, problem = _parse(text)
    return value, problem


def _parse(text: str, first: int = 1, written: bool = False) -> tuple[Any, yaml.Node | None, str | None]:
    """Parse YAML as `read_yaml` does, and give its node tree too, for the line of each key. `first` is the line of
    the file the text starts on. With `written`, each map key is the word as written."""
    try:
        loader = (_Written if written else _Strict)(text)
    except yaml.reader.ReaderError as error:  # the reader checks the whole text for characters YAML refuses
        code = error.character if isinstance(error.character, int) else 0xFFFD
        what = "control character" if unicodedata.category(chr(code)) == "Cc" else "character"
        line = len(BREAKS.findall(text, 0, error.position)) + first
        return None, None, (f"line {line}: it holds the {what} {ascii(chr(code))[1:-1]}, which YAML does not allow: "
                            "delete it")
    try:
        node = loader.get_single_node()
        return (loader.construct_document(node) if node is not None else None), node, None
    except RecursionError:
        return None, None, "it nests too deeply to read: write it flatter"
    except (TypeError, ValueError, OverflowError) as error:  # such as a date YAML reads that the calendar has not
        return None, None, f"it holds a value YAML cannot use here ({error}): write plain keys and values"
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        problem = getattr(error, "problem", None) or "it does not parse"
        return None, None, f"line {mark.line + first}: {problem}" if mark is not None else problem
    finally:
        loader.dispose()


@dataclass
class Budget:
    """What one run may spend."""

    tokens_per_run: int = 100_000
    tool_calls: int = 40


@dataclass
class Authority:
    """What a curator approves itself, as its file's `authority:` line grants it. A key the line leaves out, and every
    key of a file with no line, is false: that kind waits for a person's review. True lets the curator do it alone."""

    admit: bool = False
    archive: bool = False
    merge: bool = False
    split: bool = False


@dataclass
class Render:
    """How code writes one part of an agent's article with no model, under its heading: `collections` lists every
    collection the site shows, laid out as an `outline` or `headings`, each showing its `count` and `description` as
    `show` says. It stands `before` the agent's own text, or `after` it."""

    kind: str
    style: str = "outline"
    show: list[str] = field(default_factory=lambda: ["count"])
    place: str = "before"


@dataclass
class EvidenceLine:
    """One entry under `evidence:`: its kind, such as `query`, its arguments, and options such as `keep`."""

    kind: str
    args: Any
    options: dict[str, Any] = field(default_factory=dict)
    line: int = field(default=0, compare=False)  # its line in the file, 0 when no file gave it
    lines: dict[str, int] = field(default_factory=dict, compare=False, repr=False)  # the line of each argument


@dataclass
class Agent:
    """One context agent file, read and checked.

    `role` is `curator`, `composer`, `article`, `subagent`, `writer` (one fixed article), `query` (each article a
    `target:` query matches). `target` is
    the collection a curator curates or an article agent writes, the article a writer writes, or a query target's
    collection. `prompt` is the whole body, `instructions` the body before `# Output`, and `headings` each heading
    under `# Output` with what the file says of it. `output` is what the model writes: those headings less the ones
    `rendered:` hands to code, or a subagent's one heading. `context` is what a composer says to every subagent: its
    prompt up to its first level-one heading. `hash` is the first eight hex characters of the file's SHA-256.
    `warnings` holds the lines a file loads with but likely did not
    mean. `own` is what an agent with subagents says to itself: its prompt after its first level-one heading, but for
    `# Output`. With none, it does not run, and its subagents' content is joined.
    """

    id: str
    path: Path
    role: str
    target: str
    prompt: str
    output: list[str] = field(default_factory=list)
    evidence: list[EvidenceLine] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    heartbeat: list[str] = field(default_factory=lambda: ["change"])
    cooldown: str | None = None
    budget: Budget = field(default_factory=Budget)
    model: dict[str, Any] | None = None
    subagents: list[str] = field(default_factory=list)
    target_query: dict[str, Any] | None = None
    notice: str = ""
    instructions: str = ""
    headings: dict[str, str] = field(default_factory=dict)
    heading: str = ""
    context: str = ""
    own: str = ""
    subagent_ids: list[str] = field(default_factory=list)
    composers: list[str] = field(default_factory=list)
    authority: Authority | None = None
    rendered: dict[str, Render] = field(default_factory=dict)
    hash: str = ""
    warnings: list[str] = field(default_factory=list)


def heading_of(name: str) -> str:
    """The heading a subagent's name gives it: `overview` is Overview, and `work-streams` is Work streams."""
    words = name.replace("-", " ")
    return words[:1].upper() + words[1:]


def cooldown_seconds(value: str) -> int | None:
    """A heartbeat's `cooldown:` in seconds, or None when it is no cooldown: a duration of a minute or more, such as
    `90m`, `3h` or `1h30m`, a whole number of days from `1d` to `366d`, `hourly` or `daily`, or `0`, which waits for
    nothing, so the agent runs on every pass its evidence moves whatever the workspace's `schedule.articles` says."""
    value = str(value).strip()
    if value == "0":
        return 0
    if value in ("hourly", "daily"):
        return 3600 if value == "hourly" else 86400
    if value.endswith("d"):
        days = value[:-1]
        return int(days) * 86400 if days.isdigit() and 1 <= int(days) <= 366 else None
    parts = COOLDOWN.findall(value)
    if not parts or "".join(n + unit for n, unit in parts) != value:
        return None
    seconds = sum(float(n) * {"h": 3600, "m": 60, "s": 1}[unit] for n, unit in parts)
    return int(seconds) if seconds >= 60 else None


class _File:
    """What one load knows of the file: its name for refusals, and where it sits."""

    def __init__(self, path: Path, agents_dir: Path):
        rel = path.relative_to(agents_dir)
        self.path, self.rel = path, rel
        self.name = f"agents/{rel.as_posix()}"
        self.id = rel.with_suffix("").as_posix()
        self.parts = self.id.split("/")
        self.node: yaml.Node | None = None
        self.at: tuple[str | int, ...] = ()  # where a refusal that names no key of its own points


    def line(self, *keys: str | int) -> int:
        """The line of the file that holds the key `keys` lead to through the front matter, or of the nearest key
        above it there; 0 when the front matter was not read."""
        node, line = self.node, 0
        for key in keys:
            if isinstance(node, yaml.MappingNode):
                pair = next(((k, v) for k, v in node.value if k.value == str(key)), None)
                if pair is None:
                    break
                line, node = pair[0].start_mark.line + 2, pair[1]  # the front matter starts on line 2
            elif isinstance(node, yaml.SequenceNode) and isinstance(key, int) and 0 <= key < len(node.value):
                node = node.value[key]
                line = node.start_mark.line + 2
            else:
                break
        return line

    def fail(self, problem: str, *at: str | int) -> AgentFileError:
        at = at or self.at
        line = self.line(*at) if at else 0
        return AgentFileError(f"{self.name}: line {line}: {problem}" if line else f"{self.name}: {problem}", line)


def _place(file: _File) -> None:
    """Refuse a path the layout has no place for, with the move that fixes it."""
    parts, last = file.parts, file.parts[-1]
    into = f"agents/{parts[0]}/subagents/{last}.md"
    if len(parts) == 1:
        raise file.fail(f"it sits at the root of agents/, and every agent belongs to a collection: move it to "
                        f"agents/<collection>/{last}.md")
    if len(parts) > 3 or (len(parts) == 3 and parts[1] != "subagents"):
        raise file.fail(f"subagents/ is the only folder a collection holds: move it to {into}")
    if not ID.fullmatch(file.id):
        raise file.fail("its name is not lower case words joined by - : rename it, such as work-streams.md")


def _words(file: _File, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise file.fail(f"{key}: is not a list of names: write {key}: [first, second]")
    return [item.strip() for item in value]


def _heartbeat(file: _File, value: Any, composes: bool, curates: bool) -> tuple[list[str], str | None]:
    """The words that wake the agent and its cooldown, from the list form or the map form."""
    if value is None:
        return ([] if composes else ["change"]), None
    cooldown, is_map, on_set = None, isinstance(value, dict), False
    if is_map:
        value = {_key(key): v for key, v in value.items()}
        extra = sorted(set(value) - {"on", "cooldown"}, key=str)
        if extra:
            raise file.fail(f"heartbeat: {extra[0]}: is no key of a heartbeat, which takes on:, the words that wake "
                            "the agent, and cooldown:, how long new evidence waits after a rebuild",
                            "heartbeat", extra[0])
        if "cooldown" in value:
            cooldown = str(value["cooldown"]).strip() if value["cooldown"] is not None else ""
        on_set = "on" in value
        value = value.get("on")
    if isinstance(value, str):
        value = [value]
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(word, str) for word in value):
        raise file.fail("heartbeat: want a list such as [change, daily], or a map such as "
                        "{ on: [change], cooldown: 3h }")
    for word in value:
        if word not in HEARTBEAT_WORDS:
            raise file.fail(f"heartbeat word {word!r} is not one of {', '.join(HEARTBEAT_WORDS)}: use one of those")
    if composes:
        if value or on_set:
            raise file.fail("it lists subagents, so its run wakes when theirs would: leave out the heartbeat words, "
                            "which belong in a subagent, and write heartbeat: { cooldown: 3h } to make new evidence "
                            "wait that long after a rebuild")
        if not cooldown:
            raise file.fail("heartbeat: the map says nothing: write heartbeat: { cooldown: 3h }, or delete the "
                            "heartbeat: line")
    elif is_map and not on_set:
        if not cooldown:
            raise file.fail("heartbeat: the map says nothing: write heartbeat: { on: [change], cooldown: 3h }, or "
                            "delete the heartbeat: line")
        raise file.fail(f"heartbeat: on: is missing: name what wakes the agent, as heartbeat: {{ on: [change], "
                        f"cooldown: {cooldown} }}")
    elif not value:
        raise file.fail(f"heartbeat is empty: name at least one of {', '.join(HEARTBEAT_WORDS)}, such as [change]")
    if cooldown is not None:
        if cooldown_seconds(cooldown) is None:
            raise file.fail(f"heartbeat: cooldown {cooldown!r} is no duration of a minute or more: write one such "
                            "as 90m, 3h or 1d, or hourly or daily, or 0 to run on every pass")
        if curates:
            raise file.fail("heartbeat: cooldown makes new evidence wait after an agent rebuilds an article, and a "
                            "curator runs on no article: delete cooldown")
        if not composes and "change" not in value:
            raise file.fail(f"heartbeat: cooldown makes new evidence wait after a rebuild, and on: [{', '.join(value)}]"
                            " has no change, so new evidence never wakes the agent: add change to on:, or delete "
                            "cooldown")
    return list(value), cooldown


def _anchored(value: Any) -> bool:
    """Whether a filter value names the article a run is on, or one of its fields, such as this.linked."""
    return isinstance(value, str) and (value == "this" or value.startswith("this."))


def _query(file: _File, n: int, kind: str, value: Any, curates: bool) -> None:
    where = f"evidence {n}, {kind}"
    if not isinstance(value, dict) or not isinstance(value.get("from"), str) or not value["from"].strip():
        raise file.fail(f"{where} names no from: write - {kind}: {{ from: notes }}")
    source, filters = value["from"].strip(), {k: v for k, v in value.items() if k != "from"}
    if source == "collections" and filters:
        raise file.fail(f"{where} from collections takes no filter: it lists every collection the site shows")
    sided = False
    for key, filter_value in filters.items():
        if isinstance(filter_value, (dict, list)):
            raise file.fail(f"{where} filter {key} takes one value, not a list or a map: write {key}: <value>")
        if str(key).lower() == "limit":
            if source != "facts":
                raise file.fail(f"{where} from {source}: evidence takes no limit, since it loads every match and says"
                                " what it left out: narrow the query with a filter or a window instead")
            if isinstance(filter_value, bool) or not isinstance(filter_value, int) or filter_value < 1:
                raise file.fail(f"{where} limit {filter_value!r} is not a whole number of at least 1: write limit: 30")
        if (key == "window" or shaped(filter_value)) and not is_window(filter_value):
            raise file.fail(f"{where} {key} {filter_value!r} is no window: write one such as last_30_days, "
                            "next_14_days, before_last_365_days, this_quarter or this_fiscal_year")
        if source == "facts" and str(key).lower() == "relation" and filter_value not in RELATIONS:
            raise file.fail(f"{where} relation {filter_value!r} is not {' or '.join(RELATIONS)}: use one")
        if source == "facts" and str(key).lower() == "speaker_side":
            if str(filter_value).lower() not in ("yours", "theirs", "unknown"):
                raise file.fail(f"{where} speaker_side {filter_value!r} is not yours, theirs or unknown: use one")
            if sided:
                raise file.fail(f"{where} names speaker_side twice: keep one")
            sided = True
        if source in SITE and _anchored(filter_value):
            raise file.fail(f"{where} filter {key}: a query of the whole site cannot name this: give a value")
    if kind == "left":
        if source in SITE or source in FILES:
            what = "the whole site" if source in SITE else "a file"
            raise file.fail(f"{where} reads a collection, and {source} is {what}: query it instead")
        anchored = [k for k, v in filters.items() if _anchored(v)]
        if anchored:
            raise file.fail(f"{where} compares a query's matches from run to run, so its filter {anchored[0]} cannot "
                            "name this: give a value")
        if curates:
            raise file.fail(f"{where} compares a query's matches with the agent's last run on the article it writes,"
                            " so it needs an article, and a curator runs on no article: use query instead")


def _paths(file: _File, n: int, kind: str, value: Any) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(p, str) and p.strip() for p in value):
        raise file.fail(f"evidence {n}, {kind} needs a list of at least one path: write - {kind}: [notes]")
    for p in value:
        slashed = p.replace("\\", "/")
        if slashed.startswith("/") or ".." in slashed.split("/") or re.match(r"[A-Za-z]:", slashed):
            raise file.fail(f"evidence {n}, {kind} path {p} is outside the workspace: name a path inside it")


def _sql(file: _File, n: int, args: Any) -> None:
    """A sql line names a source by its name and holds one read: never an address or a login, which its source
    file and the secrets store hold."""
    from .connectors.database import EXAMPLE, QueryRefused, check_query  # it reads source files, which read this

    if not isinstance(args, dict) or set(args) != {"source", "query"}:
        extra = sorted(set(args) - {"source", "query"}, key=str) if isinstance(args, dict) else []
        what = (f"it names {extra[0]}, and a sql line takes source and query alone: the address and login of a "
                "database go in its source file and the secrets store" if extra else "it takes source and query")
        raise file.fail(f"evidence {n}, sql: {what}: {EXAMPLE}", "evidence", n - 1, "sql", *extra[:1])
    source = args["source"]
    if not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", source):
        raise file.fail(f"evidence {n}, sql source {source!r} is not the name of a source: name the file under "
                        "sources/ without .yaml, as source: warehouse")
    try:
        check_query(args["query"])
    except QueryRefused as error:
        raise file.fail(f"evidence {n}, sql query: {error}") from None


def _evidence(file: _File, value: Any, curates: bool) -> list[EvidenceLine]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise file.fail("evidence is not a list: write each line as - kind: { ... }", "evidence")
    lines = []
    for n, entry in enumerate(value, 1):
        file.at = ("evidence", n - 1)
        kinds = [key for key in entry if key not in EVIDENCE_OPTIONS] if isinstance(entry, dict) else []
        if len(kinds) > 1:
            raise file.fail(f"evidence {n} names {len(kinds)} kinds, {' and '.join(map(str, kinds))}: give each entry "
                            f"one kind, such as - query: {{ from: notes }}, and options among "
                            f"{', '.join(EVIDENCE_OPTIONS)}", "evidence", n - 1)
        if len(kinds) != 1:
            raise file.fail(f"evidence {n} names {len(kinds)} kinds: give each entry one kind, such as - query: "
                            "{ from: notes }")
        kind = str(kinds[0])
        file.at = ("evidence", n - 1, kinds[0])
        args, options = entry[kinds[0]], {k: v for k, v in entry.items() if k != kinds[0]}
        values = args.values() if isinstance(args, dict) else args if isinstance(args, list) else [args]
        if chain := next((v for v in values if isinstance(v, str) and v.strip().startswith("this.")
                          and v.strip().count(".") > 1), None):
            raise file.fail(f"evidence {n}, {kind}: {chain} reads a field of a field, and a value names this or one "
                            "field of it, such as this.linked: name a field this article holds")
        if kind == "facts":
            args = {} if args is None else args
            extra = sorted(set(args) - set(FACTS_KEYS), key=str) if isinstance(args, dict) else []
            if extra:
                raise file.fail(f"evidence {n}, facts takes {', '.join(FACTS_KEYS)}, not {extra[0]}: write - facts: "
                                "{ window: last_90_days }", "evidence", n - 1, kind, extra[0])
            if not isinstance(args, dict):
                raise file.fail(f"evidence {n}, facts takes {', '.join(FACTS_KEYS)}: write - facts: "
                                "{ window: last_90_days }")
            if "window" in args and not is_window(args["window"]):
                raise file.fail(f"evidence {n}, facts window {args['window']!r} is no window: write one such as "
                                "last_30_days or this_quarter")
            classes = args.get("classes", [])
            if not isinstance(classes, (str, list)) or (isinstance(classes, list)
                                                        and not all(isinstance(c, str) for c in classes)):
                raise file.fail(f"evidence {n}, facts classes is not a class or a list of them: write classes: "
                                "[timeline]")
            relation = args.get("relation", [*RELATIONS])
            if not (relation in RELATIONS or (isinstance(relation, list) and relation
                                              and all(r in RELATIONS for r in relation))):
                raise file.fail(f"evidence {n}, facts relation is {relation!r}: write {' or '.join(RELATIONS)}, or "
                                "a list of them, as relation: subject")
            for flag in ("mentions", "latest"):
                if flag in args and not isinstance(args[flag], bool):
                    raise file.fail(f"evidence {n}, facts {flag} is {args[flag]!r}: write true or false")
        elif kind in ("articles", "files"):
            _paths(file, n, kind, args)
        elif kind in ("query", "left"):
            _query(file, n, kind, args, curates)
        elif kind == "sql":
            _sql(file, n, args)
        elif kind == "held":  # the records held for a curator's collection: held.py reads and checks the line
            from .held import check_line
            extra = sorted(set(args) - {"limit"}, key=str) if isinstance(args, dict) else []
            at = ("evidence", n - 1, kind, extra[0]) if extra else ()
            args = check_line(args, curates,
                              lambda problem, n=n, at=at: file.fail(f"evidence {n}, held: {problem}", *at))
        elif kind == "script":
            if not isinstance(args, dict) or not isinstance(args.get("run"), str) or not args["run"].strip():
                raise file.fail(f"evidence {n}, script takes run, the script's path in the workspace's folder: write "
                                "- script: { run: scripts/usage.py }", "evidence", n - 1, kind)
            extra = sorted(set(args) - {"run"}, key=str)
            if extra:
                raise file.fail(f"evidence {n}, script takes run alone, not {extra[0]}: write - script: "
                                "{ run: scripts/usage.py }", "evidence", n - 1, kind, extra[0])
            run = args.get("run") if isinstance(args, dict) else None
            if isinstance(run, str) and re.search(r"[\s\]]", run.strip()):
                raise file.fail(f"evidence {n}, script run {run.strip()!r} holds a space or a ], so what it prints "
                                "could never be cited: rename the script without them")
        elif not re.fullmatch(r"[a-z][a-z0-9_-]*", kind):
            raise file.fail(f"evidence {n} kind {kind!r} is not a word: use query, facts, articles, left or files, "
                            "or the name an app gives its evidence")
        _options(file, n, kind, options)
        at = {str(key): file.line("evidence", n - 1, kinds[0], str(key)) for key in args} if isinstance(args, dict) else {}
        lines.append(EvidenceLine(kind, args, options, file.line("evidence", n - 1, kinds[0]), at))
    file.at = ()
    return lines


def _options(file: _File, n: int, kind: str, options: dict[str, Any]) -> None:
    where = f"evidence {n}, {kind}"

    def fail(option: str, problem: str) -> AgentFileError:
        return file.fail(problem, *file.at[:2], option)  # an option sits beside the kind in its entry

    summary, text_only = options.get("summary"), options.get("text_only", False)
    if ("summary" in options or "text_only" in options) and kind != "articles":
        raise fail("summary" if "summary" in options else "text_only", f"{where}: summary and text_only say how an articles entry loads each article, so they go on "
                        "an articles entry")
    if "text_only" in options and not isinstance(text_only, bool):
        raise fail("text_only", f"{where}: text_only is {text_only!r}: write text_only: true")
    if "summary" in options and (not isinstance(summary, str) or not summary.strip() or "#" in summary
                                 or "\n" in summary):
        raise fail("summary", f"{where}: summary is the heading of one part of each article, and {summary!r} is none: write summary: "
                        "Overview")
    if summary and text_only:
        raise fail("text_only", f"{where} loads each article as a summary or as text_only, not both: keep one")
    top, keep = options.get("top"), options.get("keep")
    if "top" in options and (isinstance(top, bool) or not isinstance(top, int) or top < 1):
        raise fail("top", f"{where}: top is how many passages keep loads at most, so write a number of 1 or more")
    if "top" in options and "keep" not in options:
        raise fail("top", f"{where}: top caps the passages keep loads, so it goes with keep: add keep or delete top")
    bar = options.get("keep_bar")
    if "keep_bar" in options and (isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1):
        raise fail("keep_bar", f"{where}: keep_bar is the score from 0 to 1 a passage needs to load, and {bar!r} is none: "
                        "write a number such as 0.2")
    if "keep_bar" in options and "keep" not in options:
        raise fail("keep_bar", f"{where}: keep_bar is the score keep loads a passage at, so it goes with keep: add keep or "
                        "delete keep_bar")
    if "keep" in options:
        if not isinstance(keep, str) or not keep.strip():
            raise fail("keep", f"{where}: keep is an instruction that says which evidence the run needs, and it is "
                            "empty: write one in your own words")
        if kind == "articles" and not summary and not text_only:
            raise fail("keep", f"{where}: keep chooses among the passages of articles read as text, so add text_only: "
                            "true or summary")
        if kind in ("left", "files"):
            raise fail("keep", f"{where}: keep does not go on {kind}: delete it")


def _budget(file: _File, value: Any) -> Budget:
    value = {} if value is None else value
    extra = sorted(set(value) - {"tokens_per_run", "tool_calls"}, key=str) if isinstance(value, dict) else []
    if extra:
        raise file.fail(f"budget: takes tokens_per_run and tool_calls, not {extra[0]}: write budget: "
                        "{ tool_calls: 40 }", "budget", extra[0])
    if not isinstance(value, dict):
        raise file.fail("budget: takes tokens_per_run and tool_calls, such as budget: { tool_calls: 40 }", "budget")
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise file.fail(f"budget {key} is {number!r}, not a whole number above 0: write a number such as 40",
                            "budget", key)
    return Budget(**value)


def _model(file: _File, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not (isinstance(value, dict) and all(isinstance(value.get(k), str) and value[k].strip()
                                            for k in ("provider", "model"))):
        raise file.fail("model: takes a block with the provider and the model, such as model: { provider: "
                        "fireworks, model: glm-5p3 }")
    try:
        read_model(value, "model:")
    except ModelSettingsError as error:
        extra = sorted(set(value) - set(ENTRY_KEYS), key=str)
        raise file.fail(str(error), "model", *extra[:1]) from None
    return value


def _authority(file: _File, value: Any) -> Authority | None:
    if value is None:
        return None
    extra = sorted(set(value) - set(AUTHORITY_KEYS), key=str) if isinstance(value, dict) else []
    if extra:
        raise file.fail(f"authority: takes admit, archive, merge and split, not {extra[0]}: write authority: "
                        "{ admit: false }", "authority", extra[0])
    if not isinstance(value, dict):
        raise file.fail("authority: takes admit, archive, merge and split, such as authority: { admit: false }")
    for key, flag in value.items():
        if not isinstance(flag, bool):
            raise file.fail(f"authority {key} is {flag!r}: write true, or false to send it to your review",
                            "authority", key)
    return Authority(**value)


def _rendered(file: _File, value: Any) -> dict[str, Render]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise file.fail("rendered: maps a heading to what renders it, such as rendered: { Contents: collections }")
    out = {}
    for heading, spec in value.items():
        where = f"rendered: {heading}"
        spec = {"kind": spec} if isinstance(spec, str) else spec
        if not isinstance(spec, dict):
            raise file.fail(f"{where} is a kind, such as collections, or a map of kind, style, show and place")
        extra = sorted(set(spec) - {"kind", "style", "show", "place"}, key=str)
        if extra:
            raise file.fail(f"{where} takes kind, style, show and place, not {extra[0]}: delete it", "rendered",
                            heading, extra[0])
        place = spec.get("place", "before")
        if place not in RENDER_PLACES:
            raise file.fail(f"{where}: place {place!r}: write before, to put it ahead of the agent's own text, or "
                            "after")
        kind, style, show = str(spec.get("kind", "")).strip(), spec.get("style", "outline"), spec.get("show", ["count"])
        if kind not in RENDER_KINDS:
            raise file.fail(f"{where}: {kind!r} renders nothing: write kind: {' or '.join(RENDER_KINDS)}")
        if style not in RENDER_STYLES:
            raise file.fail(f"{where}: style {style!r}: write {' or '.join(RENDER_STYLES)}")
        show = [show] if isinstance(show, str) else show
        if not isinstance(show, list):
            raise file.fail(f"{where}: show is a list, such as [count, description]")
        for i, word in enumerate(show):
            if word not in RENDER_SHOWS:
                raise file.fail(f"{where}: show {word!r}: write {' or '.join(RENDER_SHOWS)}")
            if word in show[:i]:
                raise file.fail(f"{where}: show names {word} twice: name it once")
        out[str(heading)] = Render(kind, style, list(show), place)
    return out


def _target(file: _File, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise file.fail(f"target: {value} is a path, and a file writes where its folder says: delete the target: "
                        "line, move the file for another collection, or write a query such as { from: meetings }")
    source = value.get("from")
    if not isinstance(source, str) or not source.strip():
        raise file.fail("target: is a query with no from: write target: { from: notes, status: open }")
    source = source.strip().removeprefix("articles/").strip("/")
    if source in FILES or source in SITE:
        raise file.fail(f"target from {source}: an agent writes articles, so a query target names a collection")
    if not source or "/" in source or source in (".", ".."):
        raise file.fail(f"target from {value['from']!r}: name one collection, a folder directly under articles/")
    for key, filter_value in value.items():
        if key == "window":
            raise file.fail("target: window selects a file's lines, so filter a date field with a window instead, "
                            "as start: next_14_days")
        if key == "order_by":
            raise file.fail("target: order_by orders a query's matches, and a target only picks the articles an "
                            "agent runs on: take order_by off the target")
        if _anchored(filter_value):
            raise file.fail(f"target filter {key}: {filter_value} names the article an agent runs on, and a target "
                            "picks that article: give a value")
        if isinstance(filter_value, (dict, list)):
            raise file.fail(f"target filter {key} takes one value: write {key}: <value>")
    return {**value, "from": source}


def _fenced(body: str) -> list[tuple[str, bool]]:
    """Each line of a body, and whether it sits inside a code fence or an HTML comment."""
    out, fence, comment = [], "", False
    for line in body.splitlines():
        text = line.rstrip("\r")
        trimmed = text.lstrip(" ")
        indent = len(text) - len(trimmed)
        if comment:
            out.append((text, True))
            comment = "-->" not in text
        elif fence:
            out.append((text, True))
            if indent <= 3 and trimmed.startswith(fence) and not trimmed.lstrip(fence[0]).strip():
                fence = ""
        elif indent <= 3 and trimmed.startswith("<!--"):
            out.append((text, True))
            comment = "-->" not in trimmed
        elif indent <= 3 and (match := FENCE.match(trimmed)):
            out.append((text, True))
            fence = match.group(1)
        else:
            out.append((text, indent > 3))
    return out


def _body(body: str) -> tuple[str, str, dict[str, str]]:
    """The body's shared context (up to its first level-one heading), its instructions (up to `# Output`), and each
    level-two heading under `# Output` with the lines under it."""
    context, instructions, headings = None, None, {}
    inside, heading, kept = False, None, []
    lines = _fenced(body)
    for n, (text, hidden) in enumerate(lines):
        found = None if hidden else atx(text)
        if found is not None and found[0] == 1:
            if context is None:
                context = "\n".join(line for line, _ in lines[:n]).strip()
            inside = found[1].lower() == "output"
            if inside and instructions is None:
                instructions = "\n".join(line for line, _ in lines[:n]).strip()
            if heading is not None:
                headings[heading] = "\n".join(kept).strip()
            heading = None
        elif inside and found is not None and found[0] == 2 and found[1]:
            if heading is not None:
                headings[heading] = "\n".join(kept).strip()
            heading, kept = found[1], []
        elif heading is not None:
            kept.append(text)
    if heading is not None:
        headings[heading] = "\n".join(kept).strip()
    return context if context is not None else body.strip(), instructions or body.strip(), headings


def load_agent(path: Path, agents_dir: Path) -> Agent:
    """Read and check one agent file under `agents_dir`."""
    file = _File(path, agents_dir)
    _place(file)
    try:
        data = path.read_bytes()
        text = data.decode("utf-8").removeprefix("﻿")
    except (OSError, UnicodeDecodeError) as error:
        raise file.fail(f"cannot be read as UTF-8 text ({type(error).__name__}): save it as UTF-8")
    match = FRONT.match(text)
    if match is None:
        raise file.fail("has no front matter: start the file with --- then its settings, then ---")
    if not match.group(1).strip():
        raise file.fail("front matter is empty: write its settings between the --- lines, such as heartbeat: "
                        "[change]")
    front, file.node, problem = _parse(match.group(1), first=2, written=True)
    if problem is not None:
        fix = "" if problem.endswith(": delete it") else ": fix that line or quote its value"
        at = re.match(r"line (\d+): ", problem)
        raise AgentFileError(f"{file.name}: front matter is not YAML at {problem}{fix}", int(at[1]) if at else 0)
    if not isinstance(front, dict):
        raise file.fail("front matter is not a map of settings: write key: value lines")
    front = {_key(key): value for key, value in front.items()}
    for key in front:
        if key not in KEYS:
            raise file.fail(f"front matter key {key} is not one an agent file takes: use one of {', '.join(KEYS)}", key)
    body = match.group(2).strip()
    parts, collection, last = file.parts, file.parts[0], file.parts[-1]
    is_subagent = len(parts) == 3
    file.at = ("tools",)
    tools = _words(file, "tools", front.get("tools"))
    curates = last == "curator" and not is_subagent
    if curates and "tools" in front:
        raise file.fail("a curator is given every curator tool, fetch where a source answers questions, and every app "
                        "tool: delete the tools: line")
    file.at = ("subagents",)
    names = _words(file, "subagents", front.get("subagents"))
    if "subagents" in front and front["subagents"] is not None and not names:
        raise file.fail("subagents needs at least one name, such as overview for subagents/overview.md beside it")
    composes = bool(names)
    file.at = ("target",)
    target_query = _target(file, front.get("target"))
    if curates and target_query is not None:
        raise file.fail("a curator keeps the articles of its folder, so it writes no query: delete the target: line",
                        "target")
    file.at = ("authority",)
    authority = _authority(file, front.get("authority"))
    if authority is not None and not curates:
        raise file.fail("authority is a curator's: this file curates no collection, so drop it")
    file.at = ("heartbeat",)
    heartbeat, cooldown = _heartbeat(file, front.get("heartbeat"), composes, curates)
    evidence = _evidence(file, front.get("evidence"), curates)
    context, instructions, by_heading = _body(body)
    headings = list(by_heading)
    file.at = ("rendered",)
    rendered = _rendered(file, front.get("rendered"))
    file.at = ("heading",)
    heading = front.get("heading")
    if heading is not None and (not isinstance(heading, str) or not heading.strip()):
        raise file.fail("heading: is the heading a subagent writes: write heading: Recent activity")
    if heading is not None and not is_subagent:
        raise file.fail("heading: is a subagent's, and this file is not under subagents/: delete the heading: line")
    file.at = ()
    own = instructions[len(context):].strip() if composes and instructions.startswith(context) else ""
    if not any(line.strip() and (atx(line) or (0,))[0] != 1 for line in own.splitlines()):
        own = ""  # headings with nothing under them instruct no model
    if composes:
        if target_query is not None:
            raise file.fail("target: makes it a query agent, which writes each article it matches itself, and a "
                            "query agent runs no subagents: delete subagents: and write those parts in its own "
                            f"instructions, or move the subagents to agents/{collection}/article.md, which runs them "
                            "for every article of the collection")
        if is_subagent:
            raise file.fail("a subagent returns one part of its article, so it lists no subagents of its own: delete "
                            "the subagents: key")
        if last == "curator":
            raise file.fail("a curator decides which articles exist and composes none: move subagents: to article.md")
        for key, set_ in (("tools", tools), ("authority", authority)):
            if set_:
                raise file.fail(f"it lists subagents, so its run wakes when theirs would and takes the ordinary read "
                                f"tools after them: leave out {key}, which belongs in a subagent")
        if not own:
            for key in ("evidence", "model", "budget"):
                if front.get(key) is not None:
                    raise file.fail(f"it joins its subagents' content with no model, so nothing reads {key}: move it "
                                    "to the subagent that needs it, or write its own instructions after a level-one "
                                    "heading")
            if headings:
                raise file.fail("# Output says what a model writes, and this file joins its subagents' content with "
                                "no model: delete # Output, or write its own instructions after a level-one heading")
        for i, name in enumerate(names):
            if not ID.fullmatch(name) or "/" in name:
                raise file.fail(f"subagents: {name!r}: want the bare name of a file under subagents/, such as "
                                "overview")
            if name in names[:i]:
                raise file.fail(f"subagents: {name} is listed twice: list it once")
            if not (path.parent / "subagents" / f"{name}.md").is_file():
                raise file.fail(f"it lists the subagent {name}, and agents/{collection}/subagents/{name}.md does not"
                                " exist: add the file, or take the name off the subagents: list")
        if rendered:
            raise file.fail("rendered: is for a file that writes its own article, and this one composes its article "
                            "from subagents: delete the rendered: key")
    if is_subagent:
        if rendered:
            raise file.fail("rendered: is for a file that writes its own article, and a subagent returns one part of "
                            "one: delete the rendered: key")
        heading = (heading or heading_of(last)).strip()
        output = [heading]
    else:
        output = [h for h in headings if h not in rendered]
    if not is_subagent and last not in ("article", "curator") and (path.parent / "article.md").is_file():
        raise file.fail(f"it sits beside agents/{collection}/article.md, which composes every article of the collection:"
                        f" move it to agents/{collection}/subagents/{last}.md and list it under subagents:, or to a "
                        "folder of its own")
    if is_subagent:
        role, target = "subagent", collection
    elif last == "curator":
        role, target = "curator", collection
    elif target_query is not None:
        role, target = "query", target_query["from"]
    elif last == "article":
        role, target = ("composer" if composes else "article"), collection
    else:
        role, target = ("composer" if composes else "writer"), file.id
    unused = {option for line in evidence for option in line.options
              if option in ("keep", "top", "keep_bar") and line.kind not in KEEP_ACTED_ON}
    warnings = []
    schedules = [w for w in heartbeat if w in SCHEDULES]
    if (role in ("writer", "article", "query") and schedules and "change" not in heartbeat
            and not any(line.kind in ("query", "left") for line in evidence)):
        want = "change, manual" if "manual" in heartbeat else "change"
        warnings.append(f"{file.name}: heartbeat [{', '.join(heartbeat)}] rebuilds each article every "
                        f"{ {'hourly': 'hour', 'daily': 'day', 'weekly': 'week'}[schedules[0]]} even when no new "
                        f"evidence came in: to rebuild only on new evidence, with a cooldown as long, write heartbeat:"
                        f" {{ on: [{want}], cooldown: {SCHEDULES[schedules[0]]} }}")
    file.at = ("model",)
    return Agent(
        id=file.id, path=path, role=role, target=target, prompt=body, output=output, evidence=evidence, tools=tools,
        heartbeat=heartbeat, cooldown=cooldown, budget=_budget(file, front.get("budget")),
        model=_model(file, front.get("model")), subagents=names, target_query=target_query,
        notice=f"{file.name}: read but not acted on: {', '.join(sorted(unused))}" if unused else "",
        instructions=instructions, headings=by_heading, heading=heading or "", context=context if composes else "",
        own=own,
        subagent_ids=[f"{collection}/subagents/{name}" for name in names], authority=authority,
        rendered=rendered, hash=hashlib.sha256(data).hexdigest()[:8], warnings=warnings,
    )


def refused_ids(refused: Iterable[str]) -> set[str]:
    """The id of each agent whose file a line of `refused` refuses: an agent that is there and does not load, which is
    not one whose file is gone."""
    return {line.split(".md: ", 1)[0].removeprefix("agents/") for line in refused
            if line.startswith("agents/") and ".md: " in line}


def refusal_of(refused: Iterable[str], id: str) -> str:
    """What refuses the file of the agent `id`, the line less the file's name, or empty when no line refuses it."""
    name = f"agents/{id}.md: "
    return next((line.removeprefix(name) for line in refused if line.startswith(name)), "")


def _same_heading(composer: Agent, by_id: dict[str, Agent]) -> str:
    """A line refusing `composer` when two of its subagents write under one heading, or empty when none do."""
    seen: dict[str, Agent] = {}
    for sub in (by_id[id] for id in composer.subagent_ids if id in by_id):
        key = sub.output[0].casefold() if sub.output else ""
        if (other := seen.get(key)) is not None:
            return (f"agents/{composer.id}.md: its subagents agents/{other.id}.md and agents/{sub.id}.md both write "
                    f"under the heading {sub.output[0]}, so one part would replace the other: give one of them a "
                    "heading: line of its own, or take one off subagents:")
        seen[key] = sub
    return ""


def _lists(path: Path) -> list[str] | None:
    """The subagents a file that does not load lists, when its front matter still reads as a map, or None."""
    try:
        match = FRONT.match(path.read_text(encoding="utf-8").removeprefix("\ufeff"))
    except (OSError, UnicodeDecodeError):
        return None
    front = _parse(match.group(1), written=True)[0] if match else None
    if not isinstance(front, dict):
        return None
    names = front.get("subagents")
    if names is None:
        return []
    return [str(name).strip() for name in names] if isinstance(names, list) else None


def load_agents(agents_dir: Path) -> tuple[list[Agent], list[str]]:
    """Every agent file under `agents_dir` that loads, in path order, and one line for each that does not.

    A composer is resolved to its subagents: each subagent names the composers that list it, and one no composer
    lists loads with a warning, since it does not run.
    """
    agents, refused, gone = [], [], {}
    for path in sorted(agents_dir.rglob("*.md")) if agents_dir.is_dir() else []:
        try:
            agents.append(load_agent(path, agents_dir))
        except AgentFileError as error:
            refused.append(str(error))
            gone[path.relative_to(agents_dir).with_suffix("").as_posix()] = (_lists(path), error.line)
    by_id = {agent.id: agent for agent in agents}
    for composer in [a for a in agents if a.subagent_ids]:
        if clash := _same_heading(composer, by_id):
            refused.append(clash)
            agents.remove(composer)
            del by_id[composer.id]
            gone[composer.id] = (composer.subagents, 0)
    for composer in [a for a in agents if a.subagent_ids]:
        for sub in composer.subagent_ids:
            if sub in by_id:
                by_id[sub].composers.append(composer.id)
    for agent in agents:
        if agent.role == "subagent" and not agent.composers:
            agent.warnings.append(_listed_by_none(agent, gone))
    return agents, refused


def _listed_by_none(agent: Agent, refused: dict[str, tuple[list[str] | None, int]]) -> str:
    """Why a subagent no loaded composer lists does not run. `refused` holds each file of the workspace that is refused,
    with the subagents it lists, None when its front matter does not read, and the line its refusal names."""
    collection, name = agent.id.split("/")[0], agent.id.rsplit("/", 1)[1]
    article = f"{collection}/article"
    if article not in refused and (agent.path.parent.parent / "article.md").is_file():
        refused = {}  # it loads, and beside it no other file composes: the subagent is listed there or nowhere
    near = {id: held for id, held in refused.items()
            if id.startswith(f"{collection}/") and id.count("/") == 1 and not id.endswith("/curator")}

    def at(id: str) -> str:
        return f" at line {near[id][1]}" if near[id][1] else ""

    if listing := [id for id, (names, _) in near.items() if names and name in names]:
        return (f"agents/{agent.id}.md: agents/{listing[0]}.md lists it and is refused{at(listing[0])}, so it does not "
                "run: fix that file")
    if unread := [id for id, (names, _) in near.items() if names is None]:
        if article in unread:
            return f"agents/{agent.id}.md: agents/{article}.md is refused{at(article)}, so it does not run: fix that file"
        return (f"agents/{agent.id}.md: no composer that loads lists it, so it does not run: agents/{unread[0]}.md is "
                f"refused{at(unread[0])} and may list it: fix that file")
    return (f"agents/{agent.id}.md: no composer lists it, so it does not run: list {name} under subagents: in "
            f"agents/{collection}/article.md, where its heading goes")


CHECKED = ("query", "left", "facts", "articles", "files", "sql", "held", "script")  # kinds load_agent checks itself


def _arguments(fn: Any) -> tuple[list[str], list[str]] | None:
    """The arguments an evidence function takes from its line, and those it needs, or None when it cannot be read."""
    try:
        params = inspect.signature(bind(fn, None, None), eval_str=True).parameters.values()  # type: ignore[arg-type]
    except (TypeError, ValueError, NameError):
        return None
    return [p.name for p in params], [p.name for p in params if p.default is p.empty]


def _app_line(n: int, line: EvidenceLine, fn: Any) -> tuple[str, int]:
    """Why an evidence line of an app's kind would not reach its function as written, and the line of the file that
    says so, or empty when it would."""
    if line.args is not None and not isinstance(line.args, dict):
        return (f"evidence {n}, {line.kind} takes a map of the arguments its function takes, not {line.args!r}: write "
                f"- {line.kind}: {{}} for none, or - {line.kind}: {{ <argument>: <value> }}"), line.line
    read = _arguments(fn) if callable(fn) else None
    if read is None:
        return "", 0
    takes, needs = read
    args = line.args or {}
    if extra := sorted(str(key) for key in args if str(key) not in takes):
        shown = ", ".join(f"{name}: <value>" for name in takes)
        fix = f"{', '.join(takes)}, not {extra[0]}: write - {line.kind}: {{ {shown} }}" if takes else \
            f"no argument, not {extra[0]}: write - {line.kind}: {{}}"
        return f"evidence {n}, {line.kind} takes {fix}", line.lines.get(extra[0], line.line)
    if missing := [name for name in needs if name not in args]:
        return (f"evidence {n}, {line.kind} names no {missing[0]}, which its function needs: write - {line.kind}: "
                f"{{ {missing[0]}: <value> }}"), line.line
    return "", 0


def refuse_unknown_kinds(agents: list[Agent], given: Iterable[str] | Mapping[str, Any],
                         notices: list[str]) -> tuple[list[Agent], list[str], list[str]]:
    """The agents whose every evidence line names a kind that is `given`, by Cerebro or an app of the workspace, or
    `held`, and whose every line of an app's kind holds a map of its function's arguments; one line refusing each
    other agent, at its first such line; and `notices` less each that names a refused agent's file, with a line for
    each subagent a refused composer leaves. A refused subagent leaves its composer as a subagent file that does not
    load does. When `given` maps a kind to its function, a line is checked against the arguments the function takes."""
    functions = dict(given) if isinstance(given, Mapping) else {}
    known = sorted({*given, "held"})

    def problem(agent: Agent) -> tuple[str, int]:
        for n, line in enumerate(agent.evidence, 1):
            if line.kind not in known:
                near = difflib.get_close_matches(line.kind, known, n=1)
                fix = f"did you mean {near[0]}?" if near else f"use one of {', '.join(known)}"
                return f"evidence kind {line.kind} is not one Cerebro or an app of this workspace gives: {fix}", line.line
            if line.kind not in CHECKED and (found := _app_line(n, line, functions.get(line.kind)))[0]:
                return found
        return "", 0

    return _refusing(agents, problem, notices)


def refuse_unknown_tools(agents: list[Agent], given: Iterable[str],
                         notices: list[str]) -> tuple[list[Agent], list[str], list[str]]:
    """The agents whose every tool is one Cerebro or an app of the workspace runs in their file: `query`, `read` and
    the app tools `given`, as a curator lists none; one line refusing each other agent, at its first such tool; and
    `notices` as `refuse_unknown_kinds` leaves them."""
    from .run import HAND_INS  # imported here: run imports this module

    def problem(agent: Agent) -> tuple[str, int]:
        runs = list(dict.fromkeys([*READ_TOOLS, *given]))
        for name in agent.tools:
            if name in runs:
                continue
            if name in HAND_INS:
                return f"tool {name} is how a run hands in what it wrote, and no file lists it: take it off tools:", 0
            if name == "override":
                return "tool override is one every run has already: take it off tools:", 0
            if name in CURATOR_TOOLS:
                return f"tool {name} is one every curator.md is given, and no other file runs: take it off tools:", 0
            near = difflib.get_close_matches(name, runs, n=1)
            fix = f"did you mean {near[0]}?" if near else f"use one of {', '.join(runs)}"
            return f"tool {name} is not one Cerebro or an app of this workspace runs: {fix}", 0
        return "", 0

    return _refusing(agents, problem, notices)


def _refusing(agents: list[Agent], problem: Callable[[Agent], tuple[str, int]],
              notices: list[str]) -> tuple[list[Agent], list[str], list[str]]:
    """The agents `problem` finds nothing in; a line refusing each other, at the line it names; and `notices` less each
    that names a refused agent's file, with a line for each subagent a refused composer leaves."""
    kept, refused, gone = [], [], {}
    for agent in agents:
        said, at = problem(agent)
        if not said:
            kept.append(agent)
            continue
        refused.append(f"agents/{agent.id}.md: {f'line {at}: ' if at else ''}{said}")
        gone[agent.id] = (agent.subagents, at)
    named = tuple(f"agents/{id}.md:" for id in gone)
    notices = [line for line in notices if not line.startswith(named)] if named else list(notices)
    for agent in kept:
        if gone.keys() & set(agent.composers):
            agent.composers = [id for id in agent.composers if id not in gone]
            if not agent.composers:
                agent.warnings.append(_listed_by_none(agent, gone))
                notices.append(agent.warnings[-1])
    return kept, refused, notices
