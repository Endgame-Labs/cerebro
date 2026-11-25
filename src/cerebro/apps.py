"""Tool exposure, and what an app declares to plug in.

A tool is a plain typed function with a docstring. `@agent_tool`, `@mcp_tool`, and `@evidence("name")` mark it for a
surface, and marking does nothing else: there is no registry. A function reaches a surface only when an app object
collects it from a workspace it loads, and only the surfaces it is marked for. One function may carry several marks.

A tool may declare a parameter typed `Caller` and one typed `Workspace`. Each surface fills them in with `bind`, and the
model or client never sees them. A tool may take and return `File`.

An app declares a name, the kinds it keeps, its marked functions, and a folder of context agents. It is a folder
whose `app.py` holds `app = App(...)`; a relative agents folder is read from the app's own folder. A workspace loads
one from its `extensions.py`, as `notes = load_app("apps/notes")`, and `apps_in` finds each app a module holds. Two
apps of one name, two that declare one kind, and a kind that is already a source of the workspace's are refused.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

from .caller import Caller
from .errors import CerebroError
from .store import LOGS
from .workspace import Workspace

__all__ = [
    "RESERVED", "App", "AppError", "Exposed", "ToolError", "agent_tool", "apps_in", "bind", "check_app", "check_apps",
    "check_tool", "collect", "evidence", "load_app", "marked_in", "marks",
]

WORD = re.compile(r"[a-z][a-z0-9_-]*")
LOOP_MODULES = ("pydantic_ai", "mcp")
RESERVED = ("facts", "collections", *LOGS)  # sources query already reads: no kind may take one


class ToolError(CerebroError, TypeError):
    """A function cannot be a tool as written."""


class AppError(CerebroError, ValueError):
    """An app's declaration does not load."""


@dataclass
class Marks:
    surfaces: set[str] = field(default_factory=set)
    evidence: str | None = None


def marks(fn: Callable[..., Any]) -> Marks:
    """The surfaces a function is marked for. An unmarked function has none."""
    return getattr(fn, "__cerebro_marks__", None) or Marks()


def _mark(fn: Callable[..., Any]) -> Marks:
    if not inspect.isfunction(fn):
        raise ToolError(f"{fn!r} is not a plain function: mark a def, not a class, method, or lambda object")
    found = getattr(fn, "__cerebro_marks__", None)
    if found is None:
        found = Marks()
        fn.__cerebro_marks__ = found  # type: ignore[attr-defined]
    return found


def agent_tool[F: Callable[..., Any]](fn: F) -> F:
    """Mark a function as a tool context agents may list under `tools`."""
    _mark(fn).surfaces.add("agent")
    return fn


def mcp_tool[F: Callable[..., Any]](fn: F) -> F:
    """Mark a function as a tool for outside agents, on the workspace's MCP server."""
    _mark(fn).surfaces.add("mcp")
    return fn


def evidence[F: Callable[..., Any]](name: str) -> Callable[[F], F]:
    """Mark a function as an evidence line an agent file can name, as `- <name>: { arg: value }`."""
    if not isinstance(name, str) or not WORD.fullmatch(name):
        raise ToolError(f"evidence name {name!r} is not a word: use lower case letters, digits, - and _")

    def mark(fn: F) -> F:
        _mark(fn).evidence = name
        return fn

    return mark


def _loop_types(annotation: Any) -> list[str]:
    found = []
    module = getattr(annotation, "__module__", "") or ""
    if module.split(".")[0] in LOOP_MODULES:
        found.append(f"{module}.{getattr(annotation, '__qualname__', annotation)}")
    for arg in typing.get_args(annotation):
        found += _loop_types(arg)
    return found


def check_tool(fn: Callable[..., Any]) -> None:
    """Refuse a function that is not a plain typed function with a docstring, or that takes or returns loop types."""
    name = getattr(fn, "__name__", repr(fn))
    if not inspect.isfunction(fn):
        raise ToolError(f"tool {name} is not a plain function: write it as a def")
    if not (fn.__doc__ or "").strip():
        raise ToolError(f"tool {name} has no docstring: add one that says what it does, for the model to read")
    try:
        signature = inspect.signature(fn, eval_str=True)
    except (NameError, TypeError) as error:
        raise ToolError(f"tool {name} has annotations that do not resolve ({error}): import the types it names")
    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise ToolError(f"tool {name} takes *{param.name}: name each argument it takes")
        if param.annotation is param.empty:
            raise ToolError(f"tool {name} argument {param.name} has no type: annotate it, such as {param.name}: str")
    annotations = [p.annotation for p in signature.parameters.values()] + [signature.return_annotation]
    for annotation in annotations:
        if loop := _loop_types(annotation):
            raise ToolError(f"tool {name} names {loop[0]}: a tool takes and returns plain types only")


def bind(fn: Callable[..., Any], caller: Caller, workspace: Workspace | None) -> Callable[..., Any]:
    """`fn` with its `Caller` and `Workspace` parameters filled in and left out of its signature."""
    signature = inspect.signature(fn, eval_str=True)
    fill = {p.name: (caller if p.annotation is Caller else workspace) for p in signature.parameters.values()
            if p.annotation in (Caller, Workspace)}
    if not fill:
        return fn

    def bound(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs, **fill)

    kept = [p for p in signature.parameters.values() if p.name not in fill]
    bound.__name__, bound.__qualname__, bound.__doc__ = fn.__name__, fn.__qualname__, fn.__doc__
    bound.__module__ = fn.__module__
    bound.__signature__ = signature.replace(parameters=kept)  # type: ignore[attr-defined]
    bound.__annotations__ = {p.name: p.annotation for p in kept} | {"return": signature.return_annotation}
    return bound


@dataclass
class Exposed:
    """Marked functions by surface, each by its name."""

    agent_tools: dict[str, Callable[..., Any]] = field(default_factory=dict)
    mcp_tools: dict[str, Callable[..., Any]] = field(default_factory=dict)
    evidence: dict[str, Callable[..., Any]] = field(default_factory=dict)


def collect(functions: Iterable[Callable[..., Any]]) -> Exposed:
    """Sort marked functions by surface. Unmarked functions are left out. Two of one name on a surface are refused."""
    exposed = Exposed()
    for fn in functions:
        found = marks(fn)
        if not found.surfaces and found.evidence is None:
            continue
        check_tool(fn)
        slots = [(exposed.agent_tools, fn.__name__, "agent")] if "agent" in found.surfaces else []
        if "mcp" in found.surfaces:
            slots.append((exposed.mcp_tools, fn.__name__, "mcp"))
        if found.evidence is not None:
            slots.append((exposed.evidence, found.evidence, "evidence"))
        for table, key, surface in slots:
            if key in table and table[key] is not fn:
                raise ToolError(f"two {surface} functions are named {key}: rename one of them")
            table[key] = fn
    return exposed


def marked_in(module: ModuleType) -> list[Callable[..., Any]]:
    """The marked functions a module defines, in the order it defines them."""
    return [value for value in vars(module).values()
            if inspect.isfunction(value) and value.__module__ == module.__name__
            and (marks(value).surfaces or marks(value).evidence)]


@dataclass
class App:
    """What an app declares: its name, the kinds it keeps, its marked functions, and its context agents' folder.

    `text_for_agents` lists the kinds whose records' text a context agent reads whole. Of every other kind an agent's
    `query` and `read` give what names a record and never its text, and its facts reach the agent as facts.

    `instructions`, given the `Workspace` handle, gives the text the MCP server sends a session when it connects, beside
    any other app's. Record text it quotes goes through `shown` first."""

    name: str
    kinds: list[str] = field(default_factory=list)
    functions: list[Callable[..., Any]] = field(default_factory=list)
    agents: Path | None = None
    text_for_agents: list[str] = field(default_factory=list)
    instructions: Callable[[Workspace], str] | None = None


def check_app(app: App) -> None:
    """Refuse an app whose declaration would not load, with a line that names the fix."""
    if not isinstance(app.name, str) or not WORD.fullmatch(app.name):
        raise AppError(f"app name {app.name!r} is not a word: use lower case letters, digits, - and _")
    for n, kind in enumerate(app.kinds):
        if not isinstance(kind, str) or not WORD.fullmatch(kind):
            raise AppError(f"app {app.name} declares kind {kind!r}, which is not a word: name it in lower case")
        if kind in RESERVED:
            raise AppError(f"app {app.name} declares kind {kind}, which query already reads as the workspace's own: "
                           "name the kind something else")
        if kind in app.kinds[:n]:
            raise AppError(f"app {app.name} declares kind {kind} twice: list it once")
    if app.instructions is not None and not callable(app.instructions):
        raise AppError(f"app {app.name} gives instructions that are not a function: give one that takes a Workspace "
                       "and returns the text")
    for kind in app.text_for_agents:
        if kind not in app.kinds:
            raise AppError(f"app {app.name} gives agents the text of kind {kind!r}, which it does not declare: list it "
                           "in kinds too")
    for fn in app.functions:
        found = marks(fn)
        if not found.surfaces and found.evidence is None:
            raise AppError(f"app {app.name} lists {getattr(fn, '__name__', fn)}, which has no mark: "
                           "add @agent_tool, @mcp_tool, or @evidence, or leave it out")
        check_tool(fn)
    if app.agents is not None and not Path(app.agents).is_dir():
        raise AppError(f"app {app.name} names agents folder {app.agents}, which is not a folder: create it or "
                       "leave agents unset")


def check_apps(apps: Iterable[App]) -> None:
    """Refuse each app that does not load, two apps of one name, and two that declare one kind."""
    names: set[str] = set()
    kinds: dict[str, str] = {}
    for app in apps:
        check_app(app)
        if app.name in names:
            raise AppError(f"two apps are named {app.name}: load it once, or rename one of them")
        names.add(app.name)
        for kind in app.kinds:
            if kind in kinds:
                raise AppError(f"apps {kinds[kind]} and {app.name} both declare kind {kind}: rename one of the kinds")
            kinds[kind] = app.name


def load_app(folder: Path | str) -> App:
    """The app a folder declares: its `app.py` holds `app = App(...)`. A relative `folder` is read from the folder
    of the file that calls this, so `load_app("apps/notes")` in a workspace's `extensions.py` reads the workspace's
    `apps/notes` wherever cerebro runs from; called from no file, it is read from the current folder. A relative
    agents folder is read from the app's folder. Loading one folder twice gives the same app, so a workspace may name it
    from more than one place."""
    given = Path(folder)
    if not given.is_absolute():
        caller = sys._getframe(1).f_globals.get("__file__")
        given = (Path(caller).resolve().parent if caller else Path.cwd()) / given
    root = given.resolve()
    path = root / "app.py"
    if not path.is_file():
        raise AppError(f"{root.name} holds no app.py: an app is a folder whose app.py declares app = App(...)")
    name = "cerebro_app_" + hashlib.sha1(str(path).encode()).hexdigest()[:16]
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[name] = module  # a dataclass in the module looks itself up here
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as error:  # noqa: BLE001 - any error in app.py is reported as one line
            del sys.modules[name]
            raise AppError(f"{root.name}/app.py does not import ({type(error).__name__}: {error}): fix it")
    loaded = getattr(module, "__cerebro_app__", None)
    if isinstance(loaded, App):
        return loaded
    found = getattr(module, "app", None)
    if not isinstance(found, App):
        raise AppError(f"{root.name}/app.py declares no app: write app = App(name=..., kinds=[...], functions=[...])")
    if found.agents is not None and not Path(found.agents).is_absolute():
        found = replace(found, agents=root / found.agents)
    check_app(found)
    module.__cerebro_app__ = found
    return found


def apps_in(module: ModuleType) -> list[App]:
    """The apps a module holds, such as a workspace's `extensions.py`, in the order it names them, each once."""
    found: list[App] = []
    for value in vars(module).values():
        if isinstance(value, App) and not any(value is app for app in found):
            found.append(value)
    check_apps(found)
    return found
