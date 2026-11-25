"""Which workspace folders this person trusts to run their code.

A workspace folder can come from anywhere, such as a clone, so Cerebro runs a workspace only from a folder its owner
trusts. Not only its code, its `extensions.py`, each file of kinds under `facts/`, each script an agent's `script:`
line runs and each command a source's `command:` starts: its agent files are instructions to a model, and its source
files say what to read and where to send questions, so a workspace with no code can still read local files and send
them out. Trust is for a workspace, not a path: a record holds the folder's real path and the workspace's own id,
which is made at first trust and kept in the workspace's store, never in a file a clone carries. It pins the store,
and the `providers:` block of settings as written, which says where model calls go and what they carry: settings
that change either are a workspace to trust again. A different workspace at a trusted path, such as another
workspace put on a container's `/workspace` volume, is not trusted, and neither is a workspace whose store moved.
The records are kept outside every workspace, in `<config>/cerebro/trusted.json`, beside the secrets store's own
folder, the file 0600 and its folder 0700. `cerebro init` trusts the folder it creates, and
`cerebro trust [folder]` trusts one after printing each file that would run.

`check` is what loading a workspace asks, before anything in its settings is acted on. `cerebro init` trusts the folder
it makes, and `cerebro trust` any other, once. `allows` lists what trusting a workspace would allow, read as text, with
nothing imported or run, for `status` and `trust` to show. A code file that is a link leading out of the workspace's
folder is refused even in a trusted folder. Library code states trust with `trusted=True`, never an environment
variable.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import CerebroError
from .secrets import secrets_dir
from .shown import LINE_CAP, shell_path, shown, shown_path

__all__ = [
    "TrustError",
    "UntrustedWorkspace",
    "acting",
    "allows",
    "check",
    "code",
    "code_files",
    "records",
    "refusal",
    "settings_id",
    "store_of",
    "trust",
    "trusted",
    "trusted_path",
    "untrust",
    "workspace_id",
]

FILE = "trusted.json"
KINDS = "facts"
CAP = 200  # characters of one value from a workspace's files in a listing
ID_KEY = "workspace-id"  # the state key of the workspace's own id in its store


class TrustError(CerebroError):
    """A workspace's code cannot run as it stands: a code file leads out of the folder, or the record does not read."""


class UntrustedWorkspace(TrustError):
    """A workspace folder whose code would run is not one this person trusts."""


def trusted_path() -> Path:
    """The file that lists the workspace folders this person trusts, beside the secrets store's folder."""
    return secrets_dir().parent / FILE


def code_files(root: Path) -> list[str]:
    """The files of the workspace at `root` that would run as code when it loads, relative to it: `extensions.py`, then
    each file of kinds under `facts/` that the facts layer would run, in name order. Links are listed as files."""
    root = Path(root)
    out = ["extensions.py"] if (root / "extensions.py").is_file() or (root / "extensions.py").is_symlink() else []
    folder = root / KINDS
    if folder.is_dir():
        out += [f"{KINDS}/{p.name}" for p in sorted(folder.glob("*.py")) if not p.name.startswith(("_", "."))]
    return out


def _front(path: Path) -> Any:
    from .agent import FRONT, read_yaml

    try:
        found = FRONT.match(path.read_text(encoding="utf-8").removeprefix("\ufeff"))
    except (OSError, UnicodeDecodeError):
        return None
    return read_yaml(found.group(1))[0] if found else None


def _scripts(agents: Path) -> list[tuple[str, str]]:
    """Each script an agent file's `script:` line runs, with the file, as the text reads and as the agent loader
    reads it, so a file the loader reads in another way than plain text hides no script."""
    from .agent import load_agents

    found = []
    for path in sorted(agents.rglob("*.md")) if agents.is_dir() else []:
        data = _front(path)
        evidence = data.get("evidence") if isinstance(data, dict) else None
        for entry in evidence if isinstance(evidence, list) else []:
            script = entry.get("script") if isinstance(entry, dict) else None
            run = script.get("run") if isinstance(script, dict) else script
            if run is not None:
                found.append((str(run).strip(), str(path.relative_to(agents))))
    for agent in load_agents(agents)[0] if agents.is_dir() else []:
        for line in agent.evidence:
            run = line.args.get("run") if line.kind == "script" and isinstance(line.args, dict) else None
            if run is not None:
                found.append((str(run).strip(), f"{agent.id}.md"))
    return list(dict.fromkeys(found))


def code(root: Path) -> list[str]:
    """Everything of the workspace at `root` that would run, each as a line that names it: its code files, then each
    script an agent's `script:` line runs, then each command a source's `command:` starts. Nothing is run or
    imported to find them: the files are read as text. Every value is made safe to print, by `shown`."""
    from .agent import read_yaml

    root = Path(root)
    found = [shown(rel, CAP) for rel in code_files(root)]
    found += [f"{shown(run, CAP)}, the script agents/{shown(rel, CAP)} runs" for run, rel in _scripts(root / "agents")]
    sources = root / "sources"
    for path in sorted(sources.glob("*.yaml")) if sources.is_dir() else []:
        try:
            data = read_yaml(path.read_text(encoding="utf-8"))[0]
        except (OSError, UnicodeDecodeError):
            continue
        command = data.get("command") if isinstance(data, dict) else None
        if command:
            found.append(f"{shown(' '.join(str(command).split()), CAP)}, the command sources/{shown(path.name, CAP)} "
                         "starts")
    return found


def _where(data: dict[str, Any]) -> str:
    """Where a source file points, with any login in a URL left out."""
    for key in ("url", "server", "command"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            plain = re.sub(r"(//)[^/@\s]*@", r"\1", " ".join(value.split()))
            return f"{key} {shown(plain, CAP)}"
    select = data.get("select")
    globs = [entry.get("glob") for entry in select if isinstance(entry, dict)] if isinstance(select, list) else []
    globs = [str(glob) for glob in globs if glob]
    return f"files {shown(', '.join(globs), CAP)}" if globs else "its service's own address"


def allows(root: Path) -> list[str]:
    """What trusting the workspace at `root` would allow, each a line: what would run, each source and where it points,
    and how many agent files would be read as instructions to a model. Read as text: nothing is imported or run."""
    from .agent import read_yaml

    root = Path(root)
    lines = [f"run       {item}" for item in code(root)]
    sources = root / "sources"
    for path in sorted(sources.glob("*.yaml")) if sources.is_dir() else []:
        try:
            data = read_yaml(path.read_text(encoding="utf-8"))[0]
        except (OSError, UnicodeDecodeError):
            data = None
        data = data if isinstance(data, dict) else {}
        lines.append(f"read      sources/{shown(path.name, CAP)}: kind {shown(data.get('kind', 'unknown'), 40)}, "
                     f"{_where(data)}")
    store = store_of(root)
    rest = {k: v for k, v in store.items() if k != "class"}
    block = _settings(root).get("store")
    given = isinstance(block, dict) and bool(block.get("path"))
    where = ((shown(store["path"], LINE_CAP) if given else shown_path(store["path"])) if store["class"] == "sqlite" else
             shown(json.dumps(rest, sort_keys=True), CAP) if rest else "")
    lines.append(f"store     class {shown(store['class'], CAP)}" + (f", {where}" if where else ""))
    from .gateway import route_lines

    lines += route_lines(_settings(root))
    agents = root / "agents"
    count = sum(1 for _ in agents.rglob("*.md")) if agents.is_dir() else 0
    lines.append(f"agents    {count} agent file{'' if count == 1 else 's'}, each instructions to a model")
    return lines


def acting(root: Path) -> list[str]:
    """What the folder at `root` holds that would act once it is a trusted workspace, as `allows` lists it, before
    any workspace is made there: its code, scripts and source commands, its sources, and its agent files. Empty
    when none."""
    root = Path(root)
    agents, sources = root / "agents", root / "sources"
    if not (code(root) or (agents.is_dir() and any(agents.rglob("*.md")))
            or (sources.is_dir() and any(sources.glob("*.yaml")))):
        return []
    return [line for line in allows(root) if not line.startswith("store ")]


def _read(path: Path) -> list[dict[str, str]]:
    """The trust records: each a folder's real path, the store its settings named, and the id of the workspace trusted
    there. A record without the store or the id trusts nothing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise TrustError(f"{path} does not read ({type(error).__name__}): fix it or delete it, then run cerebro "
                         "trust in each workspace folder you trust") from None
    if not isinstance(data, list):
        raise TrustError(f"{path} is not a list of folders: fix it or delete it, then run cerebro trust in each "
                         "workspace folder you trust")
    return [entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            and isinstance(entry.get("id"), str) and isinstance(entry.get("store"), dict)]


def _settings(root: Path) -> dict[str, Any]:
    from .agent import read_yaml

    try:
        data = read_yaml((root / "settings.yaml").read_text(encoding="utf-8").removeprefix("\ufeff"))[0]
    except (OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def settings_id(root: Path) -> str:
    """The id settings.yaml gives the workspace, which opens its secrets store, or "" when it gives none."""
    found = _settings(Path(root)).get("id")
    return str(found) if found else ""


def store_of(root: Path) -> dict[str, Any]:
    """The store a workspace's settings name, read as text and never opened: its class, and for SQLite the real path of
    its file, else the class's other settings as written."""
    from .store import DEFAULT_PATH

    root = Path(root).resolve()
    block = _settings(root).get("store")
    block = block if isinstance(block, dict) else {}
    name = block.get("class", "sqlite")
    if name == "sqlite":
        file = Path(str(block.get("path") or DEFAULT_PATH)).expanduser()
        return {"class": "sqlite", "path": str((file if file.is_absolute() else root / file).resolve())}
    found: dict[str, Any] = json.loads(json.dumps({"class": str(name), **{str(k): v for k, v in block.items()
                                                                          if k != "class"}}, default=str))
    return found


def workspace_id(root: Path, make: bool = False) -> str | None:
    """The id of the workspace at `root`, kept in its store, never in a file a clone carries: made here when `make` is
    True and the store holds none. None when the store holds none, and then no store file is made."""
    from .store import DEFAULT_PATH, open_store

    root = Path(root).resolve()
    settings = _settings(root)
    block = settings.get("store") if isinstance(settings.get("store"), dict) else {}
    if not make and block.get("class", "sqlite") == "sqlite":
        file = Path(str(block.get("path") or DEFAULT_PATH)).expanduser()
        if not (file if file.is_absolute() else root / file).is_file():
            return None
    store = open_store(root, settings)
    try:
        found = store.state(ID_KEY)  # a read, so a trust check never waits behind another process's write
        if not isinstance(found, str) and make:
            with store.exclusive():
                found = store.state(ID_KEY)
                if not isinstance(found, str):
                    found = secrets.token_hex(16)
                    store.set_state(ID_KEY, found)
        return found if isinstance(found, str) else None
    finally:
        store.close()


def trusted(root: Path) -> bool:
    """Whether the person trusts the workspace at `root`: a record holds the folder's real path, the store its
    settings named then, and the id of the workspace, which that store must hold still. A workspace whose settings
    name another store is not trusted, decided before anything is opened, so a store class a swapped workspace names
    is never imported."""
    real = Path(root).resolve()
    store = store_of(real)
    pinned = _calls(real)
    ids = {entry["id"] for entry in _read(trusted_path()) if entry["path"] == str(real)
           and entry["store"] == store and entry.get("providers", "") == pinned}
    return bool(ids) and workspace_id(real) in ids


def _calls(root: Path) -> str:
    """The `providers:` block of settings as the trust record pins it: where model calls go and what they carry, a
    secret by the name a header gives it, never its value. "" when there is none."""
    block = _settings(root).get("providers")
    return "" if block is None else json.dumps(block, sort_keys=True, default=str)


def records() -> list[dict[str, Any]]:
    """The trust records, each a folder's real path, the store its settings named and the workspace's own id."""
    return _read(trusted_path())


def _write(path: Path, kept: list[dict[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(sorted(kept, key=lambda entry: entry["path"]), out, indent=1)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _held(path: Path) -> Iterator[None]:
    """The trust file's lock, held across one change's read and its write, so a change another process makes at the
    same time waits and then reads what this one wrote: no record is lost, whatever the order. Where the system has no
    such lock, a change is made without it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - a system with no flock
            fcntl = None
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing it lets the lock go


def untrust(path: str) -> bool:
    """Drop the trust record of the folder at `path`, its real path as the record holds it, and say whether there
    was one. Every other line of the file is written back as it was."""
    file = trusted_path()
    with _held(file):
        _read(file)  # refuses a file that does not read, before anything is written
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        kept = [entry for entry in data if not (isinstance(entry, dict) and entry.get("path") == path)]
        if len(kept) == len(data):
            return False
        _write(file, kept)
        return True


def trust(root: Path) -> Path:
    """Trust the workspace at `root`: keep its folder's real path with the workspace's id, made now if its store
    holds none, in place of any record of that path, with where its model calls go and what they carry. Gives
    the path."""
    real = Path(root).resolve()
    id = workspace_id(real, make=True)
    path = trusted_path()
    pinned = _calls(real)
    with _held(path):
        kept = [entry for entry in _read(path) if entry["path"] != str(real)] + [
            {"path": str(real), "id": str(id), "store": store_of(real), **({"providers": pinned} if pinned else {})}]
        _write(path, kept)
    return real


def refusal(root: Path) -> str:
    """The one line an untrusted workspace is refused with, wherever it is loaded: one trusted before whose `providers:`
    block changed since is told so."""
    real = Path(root).resolve()
    moved = any(entry["path"] == str(real) and entry["store"] == store_of(real) and entry.get("providers", "") !=
                _calls(real) for entry in _read(trusted_path()))
    said = ("is a workspace you trusted, but where its model calls go or what they carry has changed since" if moved
            else "is not a workspace you trust")
    return (f"{root} {said}, so cerebro runs nothing of it: cerebro status --workspace {shell_path(root)} lists what "
            f"trusting it would allow, and cerebro trust {shell_path(root)} trusts it")


def check(root: Path, trusted_: bool | None = None) -> None:
    """Refuse to load the workspace at `root` unless `trusted_` is True or its owner trusts it. A code file that is
    a link leading out of the folder is refused whatever the trust."""
    root = Path(root).resolve()
    for rel in code_files(root):
        if not (root / rel).resolve().is_relative_to(root):
            raise TrustError(f"{rel} in {root} is a link that leads outside the workspace's folder, so it does not run: "
                             "put the file itself in the folder")
    if trusted_ or trusted(root):
        return
    raise UntrustedWorkspace(refusal(root))
