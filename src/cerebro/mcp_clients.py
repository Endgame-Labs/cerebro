"""The MCP clients a workspace's server entry can be added to, and how: Claude Desktop, Claude Code, Codex and Cursor.

Where each keeps its servers comes from one table, `clients`, given a home folder, so nothing here resolves a home of
its own: the caller passes it, and a test passes a scratch one. Claude Desktop and Cursor keep a JSON file with an
`mcpServers` object, Codex a TOML file with `[mcp_servers.<name>]` tables, and Claude Code a JSON file, `.claude.json`,
that it rewrites itself while it runs, so its entry is added by its own `claude mcp add`, through a
runner the caller passes.

`write` adds or replaces Cerebro's entry and nothing else: every other key and server stays, in its order, and in
Codex's file every other line, comments among them. The file is written to a partial file renamed into place, a link
written through, its mode kept. A file that does not read is refused with a line that says so, and nothing is written.
Every write is proven by reading the file back: `holds` says whether a client's config holds the entry.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CerebroError

__all__ = ["CLAUDE_CODE", "CODEX", "Client", "ClientError", "Entry", "clients", "found", "holds", "manual", "write"]

CLAUDE_DESKTOP, CLAUDE_CODE, CODEX, CURSOR = "Claude Desktop", "Claude Code", "Codex", "Cursor"
CLAUDE_CODE_DIR = "CLAUDE_CONFIG_DIR"  # moves Claude Code's config folder from the home folder, as claude reads it


class ClientError(CerebroError):
    """A client's config was not written. The line names the file and why, never a secret."""


@dataclass(frozen=True)
class Entry:
    """The server entry: its name, and the command and arguments that start `cerebro mcp` on the workspace."""

    name: str
    command: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class Client:
    """One MCP client: its name and the file it keeps its servers in."""

    app: str
    path: Path


def clients(home: Path, system: str, environ: Mapping[str, str]) -> list[Client]:
    """The clients, each with its config file under `home`: Claude Desktop where `system`
    (as `sys.platform` names it) has it, Claude Code, Codex and Cursor. Claude Code's file is under the folder
    `CLAUDE_CONFIG_DIR` names when it is set in `environ`, as claude reads it."""
    out = []
    if system == "darwin":
        out.append(Client(CLAUDE_DESKTOP, home / "Library" / "Application Support" / "Claude" /
                          "claude_desktop_config.json"))
    elif system.startswith("linux"):
        out.append(Client(CLAUDE_DESKTOP, home / ".config" / "Claude" / "claude_desktop_config.json"))
    folder = environ.get(CLAUDE_CODE_DIR) or ""
    out += [Client(CLAUDE_CODE, (Path(folder) if folder else home) / ".claude.json"),
            Client(CODEX, home / ".codex" / "config.toml"),
            Client(CURSOR, home / ".cursor" / "mcp.json")]
    return out


# -- reading


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise ClientError(f"{path} could not be read ({getattr(error, 'strerror', None) or error}), so nothing was "
                          "written") from None


class _Number(str):
    """A number in a client's JSON config, kept as the file spells it, so writing the file back changes no value
    Cerebro did not set: `1e400` stays `1e400`, never a double's `Infinity`, and `1E5` stays `1E5`."""


def _not_json(constant: str) -> None:
    raise ValueError(f"{constant} is not JSON")


def _json(path: Path, text: str | None) -> dict[str, Any]:
    """The client's config read as strict JSON, as the clients' own parsers read it: NaN and Infinity refused, and
    each number kept as written."""
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text, parse_float=_Number, parse_int=_Number, parse_constant=_not_json)
    except ValueError as error:  # a JSONDecodeError, or a constant strict JSON has not
        line = f" (line {error.lineno})" if isinstance(error, json.JSONDecodeError) else f" ({error})"
        raise ClientError(f"{path} does not read as JSON{line}, so nothing was written: fix it, or "
                          "add the entry by hand") from None
    if not isinstance(data, dict):
        raise ClientError(f"{path} is not a JSON object, so nothing was written: add the entry by hand")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ClientError(f"{path}: mcpServers is not an object, so nothing was written: add the entry by hand")
    return data


def _toml(path: Path, text: str | None) -> dict[str, Any]:
    try:
        return tomllib.loads(text or "")
    except tomllib.TOMLDecodeError as error:
        raise ClientError(f"{path} does not read as TOML ({error}), so nothing was written: fix it, or add the "
                          "table by hand") from None


def _same(found: Any, entry: Entry) -> bool:
    return isinstance(found, dict) and found.get("command") == entry.command and \
        list(found.get("args") or []) == list(entry.args)


def found(client: Client, name: str) -> dict[str, Any] | None:
    """The server the client's config holds by `name`, read afresh: at
    Claude Code's top level or under any of its projects. None for no such server, or a file that is missing or does
    not read."""
    try:
        text = _read(client.path)
        if client.app == CODEX:
            held = (_toml(client.path, text).get("mcp_servers") or {}).get(name)
            return held if isinstance(held, dict) else None
        data = _json(client.path, text)
    except ClientError:
        return None
    held = (data.get("mcpServers") or {}).get(name)
    if isinstance(held, dict):
        return held
    projects = data.get("projects") if client.app == CLAUDE_CODE else None
    for project in (projects.values() if isinstance(projects, dict) else ()):
        servers = project.get("mcpServers") if isinstance(project, dict) else None
        if isinstance(servers, dict) and isinstance(servers.get(name), dict):
            return servers[name]
    return None


def holds(client: Client, entry: Entry) -> bool:
    """Whether the client's config holds a server by the entry's name."""
    return found(client, entry.name) is not None


def _proven(client: Client, entry: Entry) -> bool:
    """Whether the client's config, read afresh, holds the entry exactly: what a write is proven by."""
    return _same(found(client, entry.name), entry)


def other_workspace(held: dict[str, Any] | None, entry: Entry) -> bool:
    """Whether a server held by the entry's name starts something else than this workspace: other arguments than the
    entry's, so most often another workspace's folder."""
    return held is not None and list(held.get("args") or []) != list(entry.args)


# -- writing


def _target(path: Path) -> Path:
    """The file a write lands in: the path, or the file a link at it names, followed whole."""
    try:
        return path.resolve(strict=False) if path.is_symlink() else path
    except (OSError, RuntimeError):
        raise ClientError(f"{path} is a link that does not resolve, so nothing was written") from None


def _replace(path: Path, text: str) -> None:
    """Write text to path whole, by a partial file renamed into place, keeping the file's mode, else 0600."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.cerebro-partial")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), mode)
            out.write(text)
        os.replace(partial, path)
    except OSError as error:
        raise ClientError(f"{path} could not be written ({error.strerror or error})") from None
    finally:
        partial.unlink(missing_ok=True)


def _json_text(path: Path, before: str | None, entry: Entry) -> str:
    data = _json(path, before)
    servers = dict(data.get("mcpServers") or {})
    kept = servers.get(entry.name) if isinstance(servers.get(entry.name), dict) else {}
    servers[entry.name] = {**kept, "command": entry.command, "args": list(entry.args)}  # a key the person added stays
    data["mcpServers"] = servers
    return _dumped(data)


def _dumped(data: dict[str, Any]) -> str:
    """`data` as JSON text, each number read from the file written back as the file spelled it: a number stands in
    the text first as a string no file holds, which is then put back as the number's own text."""
    mark, numbers = secrets.token_hex(8), []

    def held(value: Any) -> Any:
        if isinstance(value, _Number):
            numbers.append(str(value))
            return f"\x00{mark}{len(numbers) - 1}\x00"
        if isinstance(value, dict):
            return {key: held(item) for key, item in value.items()}
        if isinstance(value, list):
            return [held(item) for item in value]
        return value

    text = json.dumps(held(data), indent=2, ensure_ascii=False)
    for n, raw in enumerate(numbers):
        text = text.replace(f'"\\u0000{mark}{n}\\u0000"', raw, 1)
    return text + "\n"


def _key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def _toml_table(entry: Entry) -> str:
    args = ", ".join(json.dumps(a, ensure_ascii=False) for a in entry.args)
    return (f"[mcp_servers.{_key(entry.name)}]\ncommand = {json.dumps(entry.command, ensure_ascii=False)}\n"
            f"args = [{args}]\n")


HEADER = re.compile(r"^\s*\[\[?\s*(.*?)\s*\]\]?\s*(#.*)?$")
PROBE = "__cerebro_probe__"


def _is_table(lines: list[str], at: int) -> bool:
    """Whether the line at `at` is a table header, as TOML reads the file: the line put in place of a probe header,
    the file still reads and holds the probe's table at its top. A line inside a string that only looks like a header
    is not one."""
    probe = [*lines[:at], f"[{PROBE}]\n", *lines[at + 1:]]
    try:
        return PROBE in tomllib.loads("".join(probe))
    except tomllib.TOMLDecodeError:
        return False


def _statement(lines: list[str], key: str) -> tuple[int, int] | None:
    """Where `key = value` sits in a table's lines, as the first and last line of the statement, a value that runs
    over several lines, such as an array, included. None when the table has no such key."""
    at = next((i for i, line in enumerate(lines) if re.match(rf"\s*{re.escape(key)}\s*=", line)), None)
    if at is None:
        return None
    for end in range(at, len(lines)):
        try:
            tomllib.loads("".join(lines[at:end + 1]))
            return at, end
        except tomllib.TOMLDecodeError:
            continue
    return None


def _toml_text(path: Path, before: str | None, entry: Entry) -> str:
    """Codex's file with Cerebro's table set to the entry: its `command` and `args` lines changed in place, or the table
    added at the end, and every other line as it was, the person's own keys on that table, its subtables and every
    comment among them. Tables are found as TOML reads the file, never by a line's look. The result is read back
    before anything is written: unless it holds exactly what the file held with the entry set, it is refused."""
    text = before or ""
    data = _toml(path, text)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    own = {f"mcp_servers.{_key(entry.name)}", f"mcp_servers.{json.dumps(entry.name)}", f"mcp_servers.{entry.name}"}
    tables = [i for i, line in enumerate(lines) if HEADER.match(line) and _is_table(lines, i)]
    start = next((i for i in tables if (m := HEADER.match(lines[i])) and m.group(1) in own), None)
    if start is None:
        lead = "" if not text else "\n"
        new = "".join(lines) + lead + _toml_table(entry)
    else:
        end = next((i for i in tables if i > start), len(lines))
        body = lines[start + 1:end]
        args = "[" + ", ".join(json.dumps(a, ensure_ascii=False) for a in entry.args) + "]"
        for key, value in (("args", args), ("command", json.dumps(entry.command, ensure_ascii=False))):
            where = _statement(body, key)
            body = body[:where[0]] + [f"{key} = {value}\n"] + body[where[1] + 1:] if where \
                else [f"{key} = {value}\n", *body]
        new = "".join([*lines[:start + 1], *body, *lines[end:]])
    want = json.loads(json.dumps(data, default=str))
    servers = want.setdefault("mcp_servers", {})
    servers[entry.name] = {**(servers.get(entry.name) or {}), "command": entry.command, "args": list(entry.args)}
    try:
        got = json.loads(json.dumps(tomllib.loads(new), default=str))
    except tomllib.TOMLDecodeError:
        got = None
    if got != want:
        raise ClientError(f"{path} names the {entry.name} server in a form init does not edit, so nothing was "
                          "written: change it by hand")
    return new


def write(client: Client, entry: Entry, run: Callable[[Sequence[str]], tuple[int, str]] | None = None,
          replace: bool = False) -> None:
    """Add or update the entry in the client's config, then prove it by reading the file back. A server of the same
    name that starts something else, most often another workspace, is replaced only with `replace`, the person's say.
    Claude Code's is added with `claude mcp add --scope user` through `run`, which runs a command and gives its exit
    code and output, and never on a name it holds already, since claude refuses a name twice; after a failed add,
    `claude mcp get` is asked before it counts as failed, since claude may keep it in a config this does not read. With
    no `run`, Claude Code's entry is
    not written here. Raises ClientError with a line that says why: nothing was written, or the file was put back as
    it was."""
    held = found(client, entry.name)
    if other_workspace(held, entry) and not replace:
        raise ClientError(f"{client.app} holds {entry.name} already, starting {_words(held)}, so nothing was written: "
                          "replace it only if that workspace is gone")
    if client.app == CLAUDE_CODE:
        _claude_code(client, entry, run, held)
        return
    target = _target(client.path)
    before = _read(target)
    text = _toml_text(client.path, before, entry) if client.app == CODEX else _json_text(client.path, before, entry)
    if _read(target) != before:
        raise ClientError(f"{client.path} changed while it was read, so nothing was written: try again")
    _replace(target, text)
    if not _proven(client, entry):
        if before is None:
            target.unlink(missing_ok=True)
        else:
            _replace(target, before)
        raise ClientError(f"{client.path} did not hold the {entry.name} server when read back, so it was put back as "
                          "it was: add it by hand")


def _words(held: dict[str, Any] | None) -> str:
    return " ".join(str(w) for w in [(held or {}).get("command", ""), *((held or {}).get("args") or [])]).strip()


def _claude_code(client: Client, entry: Entry, run: Callable[[Sequence[str]], tuple[int, str]] | None,
                 held: dict[str, Any] | None) -> None:
    if held is not None:
        if _same(held, entry):
            return
        raise ClientError(f"Claude Code holds {entry.name} already, starting {_words(held)}, and claude refuses a name "
                          f"twice, so it was left as it is: run claude mcp remove --scope user {entry.name}, then add "
                          "it again")
    if run is None:
        raise ClientError("claude is not on the PATH, so Claude Code was not set up: run the command its steps show")
    code, said = run(["claude", "mcp", "add", "--scope", "user", entry.name, "--", entry.command, *entry.args])
    if code == 0 and _proven(client, entry):
        return
    got, listed = run(["claude", "mcp", "get", entry.name])
    if got == 0 and entry.args and entry.args[-1] in listed:  # claude keeps it in a config init does not read
        return
    why = " ".join(said.split())[:200] or f"exit {code}"
    raise ClientError(f"claude mcp add did not add it ({why})" if code else
                      f"claude mcp get {entry.name} did not list it, and {client.path} does not hold it")


# -- by hand


def manual(client: Client, entry: Entry, home: Path) -> list[tuple[str, str]]:
    """The steps to add the entry by hand, each a line to do and the text to copy, or "": open the file, add the entry,
    restart. The file's path is spelled from `home` as ~. For Claude
    Code, whose `claude mcp add` refuses a name it holds, a name it holds already is removed first, by the person:
    Cerebro never removes an entry. One kept for a project alone is removed in its folder."""
    try:
        where = "~/" + str(client.path.relative_to(home))
    except ValueError:
        where = str(client.path)
    if client.app == CLAUDE_CODE:
        words = " ".join(_quote(w) for w in [entry.command, *entry.args])
        add = f"claude mcp add --scope user {entry.name} -- {words}"
        project = _project_of(client, entry.name)
        if found(client, entry.name) is None:
            return [("Run this command in a terminal", add)]
        remove = (("Remove the entry Claude Code holds under this name",
                   f"claude mcp remove --scope user {entry.name}") if project is None else
                  (f"Remove the entry Claude Code holds under this name for {project}, in that folder",
                   f"cd {_quote(project)} && claude mcp remove --scope local {entry.name}"))
        return [remove, ("Then add this workspace's", add)]
    if client.app == CODEX:
        return [(f"Open {where}, and create it if it is not there yet", ""),
                ("Add this table", _toml_table(entry).rstrip("\n")),
                ("Start a new Codex session", "")]
    block = json.dumps({"mcpServers": {entry.name: {"command": entry.command, "args": list(entry.args)}}}, indent=2)
    steps = [(f"Open {where}, and create it if it is not there yet", ""),
             ("Add this server entry inside its mcpServers block, or paste the whole block into an empty file", block)]
    return steps + ([] if client.app == CURSOR else [(f"Restart {client.app}", "")])


def _project_of(client: Client, name: str) -> str | None:
    """The project folder Claude Code keeps the server `name` for alone, or None when it holds it at its top level or
    not at all."""
    try:
        data = _json(client.path, _read(client.path))
    except ClientError:
        return None
    if isinstance((data.get("mcpServers") or {}).get(name), dict):
        return None
    projects = data.get("projects")
    for folder, project in (projects.items() if isinstance(projects, dict) else ()):
        servers = project.get("mcpServers") if isinstance(project, dict) else None
        if isinstance(servers, dict) and isinstance(servers.get(name), dict):
            return str(folder)
    return None


def _quote(word: str) -> str:
    return word if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]+", word) else "'" + word.replace("'", "'\\''") + "'"
