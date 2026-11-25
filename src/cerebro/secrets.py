"""Keys and logins: one store per workspace, outside the workspace's folder, and nothing else that holds them.

A model provider's key, a source's login and a value a skill or a source's server reads from its environment all live
in one store, keyed by the workspace's id. `Secrets` is the seam: get, set, delete, list the
names and forget. `open_secrets` picks the store: the OS keychain through `keyring` where
one is usable, else a plain-text file only the owner can read, `<config>/cerebro/secrets/<id>.json`, the file 0600
and its folder 0700, where `<config>` is `$XDG_CONFIG_HOME` when that is an absolute path and `~/.config` otherwise.
`CEREBRO_SECRETS_STORE=file` forces the file. Nothing is ever written inside a workspace's folder.

The rule: a secret's value never leaves this module but through `get`. No error, `repr`, listing or location holds
one; names alone. `Scrubber` masks every stored value in a line before it is written, in the forms a log line, a URL,
a JSON document or an error may carry it.
"""

from __future__ import annotations

import base64
import bisect
import contextlib
import functools
import html
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Container, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from html.entities import codepoint2name
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from .errors import CerebroError
from .models import ModelSpec
from .shown import shell_path
from .source import SourceFile
from .tokens import hide_tokens

__all__ = [
    "MARK", "MIN_SCRUB_LENGTH", "STORE_ENV", "EveryValue", "FileSecrets", "KeychainSecrets", "Mask", "Scrubber",
    "SecretNotFound", "Secrets", "SecretsError", "changed", "check_name", "claimed_by", "claims", "env_key",
    "every_store", "forget_each", "forms", "held", "keychain_usable", "login_getter", "login_name",
    "may_hold", "mcp_token_names", "model_key", "open_secrets", "release", "scrub",
    "scrubber", "secrets_dir", "sign_in_name", "stored_ids",
]

APP = "cerebro"
STORE_ENV = "CEREBRO_SECRETS_STORE"
STORE_FILE = "file"
MARK = "[secret]"
MIN_SCRUB_LENGTH = 6
MAX_NAME_BYTES = 128
INDEX = ".names"
NO_KEYCHAIN = "this machine has no os keychain Cerebro can use, so none was looked in"
_GENERATIONS: dict[str, int] = {}  # how many times this process changed the store at each place


def changed(store: Secrets) -> None:
    """Note that this process changed `store`, so a mask of every value it holds is built again before its next use."""
    _GENERATIONS[store.where()] = _GENERATIONS.get(store.where(), 0) + 1
DIR_MODE = 0o700
FILE_MODE = 0o600
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\Z")
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TEMP_PREFIX = ".tmp-"


class SecretsError(CerebroError):
    """A secret or the store that holds it cannot be used. The message names the secret, never its value."""


class SecretNotFound(SecretsError):
    """The store holds no secret by that name."""


# -- names


def check_name(name: str) -> str:
    """Refuse a name that cannot be a provider's id, a variable a skill reads, or a source's login:
    letters, digits, `_` and `-`, not starting with a digit or `-`, at most 128 bytes."""
    if "=" in name:
        # It may be NAME=value typed in one go, so it is not repeated.
        raise SecretsError("secret name holds =, so it is not shown: a name is letters, digits, _ and - alone")
    if len(name.encode()) > MAX_NAME_BYTES:
        raise SecretsError(f"secret name of {len(name.encode())} bytes: the most is {MAX_NAME_BYTES}")
    if not _NAME.match(name):
        raise SecretsError("secret name holds what a name cannot, so it is not shown: use letters, digits, _ and -, "
                           "not starting with a digit or -")  # it may be a name and its value typed in one go
    return name


def _check_value(name: str, value: Any) -> str:
    if not isinstance(value, str) or value == "":
        raise SecretsError(f"secret {name}: the value is empty: give a value")
    if "\x00" in value:
        raise SecretsError(f"secret {name}: the value holds a nul byte, which no environment variable can carry")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise SecretsError(f"secret {name}: the value is not valid utf-8") from None
    return value


def env_key(source: str, variable: str) -> str:
    """The name a variable a source's server reads is kept under: the source's name with each dot an underscore, a
    dash, and the variable, as `notes-NOTES_TOKEN`."""
    return source.replace(".", "_") + "-" + variable


def login_name(source: SourceFile) -> str | None:
    """The name a source's login is kept under, or None for a kind that keeps none: the item its `auth: { keychain:
    <item> }` names, else the rule of its kind's connector, `Connector.login_name`."""
    from .connectors import CONNECTORS  # here: the connectors read this module

    if source.auth.startswith("keychain:"):
        return source.auth.removeprefix("keychain:").strip()
    built = CONNECTORS.get(source.kind)
    return built.login_name(source) if built is not None else None


def mcp_token_names(source: SourceFile) -> tuple[str, str]:
    """The names an `mcp` source's token may be kept under: its own name, its dots read as underscores, alone or with
    `-token`. A name that merely starts with the source's name is another secret: `lin` never reads `linear`."""
    own = source.name.replace(".", "_")
    return own, f"{own}-token"


def sign_in_name(source: SourceFile) -> str:
    """The name a source's browser sign-in is kept under: for an `mcp` source `<name>-login`, its name's dots read as
    underscores, which the rule that an `mcp` source reads only secrets named for itself admits; for a kind that reads
    its provider's MCP server the kind's own login name, where a token would be kept."""
    if source.kind == "mcp":
        return f"{source.name.replace('.', '_')}-login"
    return login_name(source) or source.kind


# -- the seam


class Secrets:
    """One workspace's secrets, keyed by name. `get` is the one way a value leaves."""

    def get(self, name: str) -> str:
        """A secret's value, or `SecretNotFound`."""
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:
        """Keep a secret, replacing any value it had."""
        raise NotImplementedError

    def delete(self, name: str) -> bool:
        """Remove a secret, and say whether there was one. Removing one that is not there is no error."""
        raise NotImplementedError

    def names(self) -> list[str]:
        """The names of the secrets kept, sorted. Never a value."""
        raise NotImplementedError

    def where(self) -> str:
        """Where the secrets live, for a message. Never a value."""
        raise NotImplementedError

    def forget(self) -> list[str]:
        """Remove every secret, and give the names removed."""
        gone = [name for name in self.names() if self.delete(name)]
        return sorted(gone)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.where()}>"


def _not_found(name: str, where: str) -> SecretNotFound:
    return SecretNotFound(f"no secret named {name} in {where}: run {APP} secrets set {name}")


# -- where


def secrets_dir() -> Path:
    """The folder that holds the file store's files and the lock both stores take."""
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(config):
        home = os.environ.get("HOME") or os.path.expanduser("~")
        if not os.path.isabs(home):
            raise SecretsError("no per-user config folder: set HOME or XDG_CONFIG_HOME to an absolute path")
        config = os.path.join(home, ".config")
    return Path(config) / APP / "secrets"


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """Make the folder, owner only, mark it out of any repository, and hold its lock file while a change is made."""
    folder.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(folder, DIR_MODE)  # mkdir leaves a folder that was there as it was, and this one is ours
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    fd = os.open(folder / ".lock", os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        try:
            import fcntl
        except ImportError:  # no advisory locks on this system: changes are not serialized
            fcntl = None
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# -- the file store


class FileSecrets(Secrets):
    """One workspace's secrets in one file, a JSON object of names and values, in plain text: with no keychain there is
    no key to encrypt with that is not on the same disk. Every write replaces the file whole, from a temp file made
    0600 in the same folder."""

    def __init__(self, folder: Path, workspace_id: str):
        self.folder = folder
        self.path = folder / f"{_check_id(workspace_id)}.json"

    def where(self) -> str:
        return str(self.path)

    def _read(self) -> dict[str, str]:
        try:
            held = os.lstat(self.path)
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise SecretsError(f"{self.path} cannot be read ({e.strerror}): check its owner and mode") from None
        if stat.S_ISLNK(held.st_mode) or not stat.S_ISREG(held.st_mode):
            raise SecretsError(f"{self.path} is a link or not a plain file, so it is not read: delete it, then run "
                               f"{APP} secrets set again")
        if held.st_mode & 0o077:
            raise SecretsError(f"{self.path} can be read or changed by others, so it is not read: run chmod 600 "
                               f"{self.path}, and check no one changed it")
        if hasattr(os, "getuid") and held.st_uid != os.getuid():
            raise SecretsError(f"{self.path} belongs to another user, so it is not read: delete it, then run {APP} "
                               f"secrets set again")
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                data = f.read()
        except OSError as e:
            raise SecretsError(f"{self.path} cannot be read ({e.strerror}): check its owner and mode") from None
        try:
            found = json.loads(data)
        except ValueError:
            found = None  # the decoder's message can quote what it choked on, which may be a value
        if not isinstance(found, dict) or not all(isinstance(v, str) for v in found.values()):
            raise SecretsError(f"{self.path}: want a json object of secret names and string values: fix or delete it")
        for name, value in found.items():
            try:
                _check_value(check_name(name), value)
            except SecretsError as e:
                raise SecretsError(f"{self.path}: {e}") from None
        return found

    def _write(self, kept: dict[str, str]) -> None:
        """Write the file whole. A store whose last secret goes keeps an empty file, so a file that is gone was
        removed by a forget or by something outside Cerebro, never by an unset."""
        text = json.dumps(dict(sorted(kept.items())), indent=2, ensure_ascii=False) + "\n"
        fd, temp = tempfile.mkstemp(prefix=_TEMP_PREFIX + self.path.name + "-", dir=self.folder)  # made 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp, FILE_MODE)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def _update(self, change: Callable[[dict[str, str]], bool]) -> None:
        with _locked(self.folder):
            for stale in self.folder.glob(_TEMP_PREFIX + self.path.name + "-*"):
                stale.unlink(missing_ok=True)  # a write that died before its rename left a full copy
            kept = self._read()
            if change(kept):
                self._write(kept)

    def get(self, name: str) -> str:
        found = self._read().get(check_name(name))
        if found is None:
            raise _not_found(name, self.where())
        return found

    def set(self, name: str, value: str) -> None:
        _check_value(check_name(name), value)

        def change(kept: dict[str, str]) -> bool:
            if kept.get(name) == value:
                return False
            kept[name] = value
            return True

        self._update(change)
        changed(self)

    def delete(self, name: str) -> bool:
        check_name(name)
        if not self.path.exists():
            return False
        removed: list[str] = []

        def change(kept: dict[str, str]) -> bool:
            if kept.pop(name, None) is None:
                return False
            removed.append(name)
            return True

        self._update(change)
        changed(self)
        return bool(removed)

    def names(self) -> list[str]:
        return sorted(self._read())

    def forget(self) -> list[str]:
        if not self.path.exists():
            return []
        with _locked(self.folder):
            gone = sorted(self._read())
            self.path.unlink(missing_ok=True)
        changed(self)
        return gone


# -- the keychain store


class KeychainSecrets(Secrets):
    """One workspace's secrets in the OS keychain through `keyring`: one item per secret, whose service is `cerebro:`
    and the workspace's id and whose user is the secret's name. A keychain cannot list one service's items, so the store
    keeps its names in one more item, `.names`, which no secret can be called. That list changes under the lock."""

    def __init__(self, backend: Any, folder: Path, workspace_id: str):
        self.backend = backend
        self.folder = folder
        self.service = f"{APP}:{_check_id(workspace_id)}"

    def where(self) -> str:
        return f"the os keychain (service {self.service})"

    def _call(self, what: str, method: str, *args: str) -> Any:
        try:
            return getattr(self.backend, method)(self.service, *args)
        except Exception as e:  # noqa: BLE001 - every backend failure becomes one line
            # A backend's own message is not passed on, in case it quotes what it was handed.
            raise SecretsError(f"the os keychain could not {what} ({type(e).__name__}): set {STORE_ENV}=file to "
                               f"use the file store instead") from None

    def _read(self, account: str) -> str | None:
        return self._call(f"read {account}", "get_password", account)

    def _remove(self, account: str) -> bool:
        if self._read(account) is None:
            return False
        self._call(f"remove {account}", "delete_password", account)
        return True

    def _index(self) -> list[str]:
        listed = self._read(INDEX) or ""
        return sorted({n for n in listed.split("\n") if _NAME.match(n)})

    def _write_index(self, names: list[str]) -> None:
        if not names:
            self._remove(INDEX)
            return
        self._call("write its list of names", "set_password", INDEX, "\n".join(sorted(set(names))))

    def get(self, name: str) -> str:
        found = self._read(check_name(name))
        if found is None:
            raise _not_found(name, self.where())
        return found

    def set(self, name: str, value: str) -> None:
        _check_value(check_name(name), value)
        with _locked(self.folder):
            self._call(f"write {name}", "set_password", name, value)
            listed = self._index()
            if name not in listed:
                self._write_index(listed + [name])
        changed(self)

    def delete(self, name: str) -> bool:
        check_name(name)
        with _locked(self.folder):
            removed = self._remove(name)
            listed = self._index()
            if name in listed:
                self._write_index([n for n in listed if n != name])
        changed(self)
        return removed

    def names(self) -> list[str]:
        # An item removed outside Cerebro leaves its name in the list, so each name is checked against its item.
        return [n for n in self._index() if self._read(n) is not None]

    def forget(self) -> list[str]:
        with _locked(self.folder):
            gone = [n for n in self._index() if self._remove(n)]
            self._remove(INDEX)
        changed(self)
        return sorted(gone)


# -- choosing a store


def _check_id(workspace_id: str) -> str:
    if not _WORKSPACE_ID.match(workspace_id or ""):
        raise SecretsError(f"workspace id {workspace_id!r}: want 1 to 64 letters, digits, _ and -, as settings.yaml id")
    return workspace_id


def keychain_usable(backend: Any) -> bool:
    """Whether a `keyring` backend is a real keychain: not the one that fails, not the one that drops what it is
    handed, and not a plain-text file of the `keyrings.alt` kind, which the file store does better."""
    module = type(backend).__module__
    if module.startswith(("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")):
        return False
    try:
        return float(backend.priority) > 0
    except Exception:  # noqa: BLE001 - a backend that cannot say its priority is not used
        return False


def _default_backend() -> Any:
    import keyring

    return keyring.get_keyring()


# which workspace folder holds each id; an id cannot start with a dot, so no store is named so
FOLDERS = ".folders.json"


def _holds_id(folder: Path, workspace_id: str) -> bool:
    """Whether the workspace at `folder` still names `workspace_id` in its settings, so it was not moved or renamed
    away."""
    try:
        import yaml

        settings = yaml.safe_load((folder / "settings.yaml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a folder whose settings do not read no longer holds the id
        return False
    return isinstance(settings, dict) and str(settings.get("id", "")) == workspace_id


def _clash(workspace_id: str, other: str, root: Path) -> str:
    return (f"workspace id {workspace_id}, the id: key in {root / 'settings.yaml'}, belongs to the workspace at {other}, and two "
            f"workspaces do not share one secrets store: run cerebro trust --new-id {shell_path(root)} to give this copy an id of its "
            "own")


def claimed_by(workspace_id: str, root: Path) -> str | None:
    """The other workspace folder that holds `workspace_id` and still names it, or None when no other one does."""
    root = Path(root).resolve()
    try:
        held = json.loads((secrets_dir() / FOLDERS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    other = held.get(workspace_id) if isinstance(held, dict) else None
    if isinstance(other, str) and other != str(root) and _holds_id(Path(other), workspace_id):
        return other
    return None


def _claim(folder: Path, workspace_id: str, root: Path) -> None:
    """Note that the workspace at `root` holds `workspace_id`, and refuse when another workspace folder that still names
    the same id holds it, so two workspaces never share one store. A folder moved, or one whose id changed, gives its
    claim up."""
    root = Path(root).resolve()
    path = folder / FOLDERS
    with _locked(folder):
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = {}
        held = held if isinstance(held, dict) else {}
        other = held.get(workspace_id)
        if other == str(root):
            return
        if isinstance(other, str) and _holds_id(Path(other), workspace_id):
            raise SecretsError(_clash(workspace_id, other, root))
        held[workspace_id] = str(root)
        fd, temp = tempfile.mkstemp(prefix=_TEMP_PREFIX + FOLDERS + "-", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(held, f, indent=2, sort_keys=True)
        os.replace(temp, path)


def claims() -> dict[str, str]:
    """Each workspace id the secrets folder records a claim for, with the folder that claimed it. Nothing for a file
    that is not there or does not read."""
    try:
        held = json.loads((secrets_dir() / FOLDERS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in held.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(held, dict) else {}


def release(workspace_id: str) -> bool:
    """Drop the claim on `workspace_id`, and say whether there was one. Other claims are written back as they were."""
    folder = secrets_dir()
    path = folder / FOLDERS
    if not path.exists():
        return False
    with _locked(folder):
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SecretsError(f"{path} does not read: fix it or delete it") from None
        if not isinstance(held, dict) or workspace_id not in held:
            return False
        del held[workspace_id]
        fd, temp = tempfile.mkstemp(prefix=_TEMP_PREFIX + FOLDERS + "-", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(held, f, indent=2, sort_keys=True)
        os.replace(temp, path)
    return True


def may_hold(folder: Path, workspace_id: str) -> bool:
    """Whether the workspace folder may hold `workspace_id`: False only when its settings.yaml is gone, or reads and
    names another id. Settings that do not read count as holding it, so nothing is removed on a doubt."""
    try:
        import yaml

        settings = yaml.safe_load((Path(folder) / "settings.yaml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 - settings there but unread: the folder may hold the id
        return True
    if not isinstance(settings, dict):
        return True
    return str(settings.get("id", "")) == workspace_id


def stored_ids() -> list[str]:
    """The workspace ids the file store holds a file for, sorted. A keychain cannot be listed: its ids come from
    claims."""
    folder = secrets_dir()
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if not p.name.startswith(".") and _WORKSPACE_ID.match(p.stem))


def every_store(workspace_id: str) -> tuple[list[Secrets], list[str]]:
    """Every store a secret of one workspace can be in on this machine, whatever `CEREBRO_SECRETS_STORE` says now, since
    it may have said otherwise when a secret was kept: the keychain where one is usable, and the file store. With them,
    a line for each store that cannot be read here, which names no secret."""
    folder = secrets_dir()
    stores: list[Secrets] = [FileSecrets(folder, _check_id(workspace_id))]
    try:
        backend = _default_backend()
        usable = keychain_usable(backend)
    except Exception as e:  # noqa: BLE001 - a keychain that cannot be reached is said, never a failure
        return stores, [f"the os keychain could not be reached ({type(e).__name__}), so it was not looked in"]
    if not usable:
        return stores, [NO_KEYCHAIN]
    keychain = KeychainSecrets(backend, folder, workspace_id)
    try:
        keychain._read(INDEX)
    except SecretsError:
        return stores, [f"{keychain.where()} could not be read, so it was not looked in"]
    return [keychain, *stores], []


def _left(store: Secrets) -> bool:
    """Whether a store still holds something of the workspace: a list of names whose items are gone, or a file."""
    return store.path.exists() if isinstance(store, FileSecrets) else store._read(INDEX) is not None


def held(workspace_id: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Where `forget_each` would remove secrets of one workspace from, each place with the names it holds, and never a
    value, from every store `every_store` gives. With them, a line for each store that cannot be read here."""
    stores, unread = every_store(workspace_id)
    found = [(each, each.names()) for each in stores]
    return [(each.where(), names) for each, names in found if names or _left(each)], unread


def open_secrets(workspace_id: str, *, root: Path | None = None) -> Secrets:
    """The store for one workspace, by the id in its settings. The keychain `keyring` picks, when it is usable and
    `CEREBRO_SECRETS_STORE` is unset; the file store otherwise. `root` is the workspace's folder, which the store must
    lie outside, and which comes to own the id: a second folder with the same id is refused."""
    _check_id(workspace_id)
    choice = os.environ.get(STORE_ENV, "")
    if choice not in ("", STORE_FILE):
        raise SecretsError(f"{STORE_ENV}={choice!r}: want {STORE_FILE}, or leave it unset")
    folder = secrets_dir()
    if root is not None and folder.resolve().is_relative_to(Path(root).resolve()):
        raise SecretsError(f"the secrets folder {folder} is inside the workspace's folder {root}: set XDG_CONFIG_HOME "
                           f"to a folder outside it")
    if root is not None:
        _claim(folder, workspace_id, root)
    if choice == "":
        backend = _default_backend()
        if keychain_usable(backend):
            return KeychainSecrets(backend, folder, workspace_id)
    return FileSecrets(folder, workspace_id)


def forget_each(workspace_id: str) -> list[tuple[str, list[str]]]:
    """Remove every secret of one workspace from every store `every_store` gives, whatever `CEREBRO_SECRETS_STORE` says
    now. Give each place something was removed from, with the names removed there."""
    stores, _ = every_store(workspace_id)
    out = []
    for each in stores:
        was = _left(each)
        gone = each.forget()
        if gone or was:
            out.append((each.where(), gone))
    return out


def login_getter(store: Secrets, source: SourceFile) -> Callable[[], str]:
    """A function that reads a source's login when it is called, for a connector to hold instead of the value."""
    name = login_name(source)
    if not name:
        raise SecretsError(f"sources/{source.name}.yaml: a {source.kind} source keeps no login in the secrets store")

    def get() -> str:
        return store.get(name)

    return get


def model_key(store: Secrets | None, spec: ModelSpec, seen: Callable[[str], None] | None = None) -> Callable[[], str]:
    """A function that reads a model's key when it is called: from the store under the provider's id,
    and, when the store holds none or no store is open, from the provider's environment variable. `seen` is
    handed each value read, for the app's scrub. An empty string means neither holds one: a blank key is no key."""
    def get() -> str:
        value = ""
        if store is not None:
            with contextlib.suppress(SecretNotFound):
                value = store.get(spec.key_name)
        value = (value or "").strip() or os.environ.get(spec.variable, "").strip()
        if value and seen is not None:
            seen(value)
        return value

    return get


# -- scrub


def _quoted_escaped(value: str) -> str:
    """The value as an escaped quoted string writes it, without the quotes: `"` and `\\` escaped, a control character
    with a letter as `\\n` or `\\t`, and any other that does not print as `\\x`, `\\u` or `\\U` and its hex code. A
    secret a log or an error writes this way is masked too."""
    named = {'"': '\\"', "\\": "\\\\", "\a": "\\a", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r",
             "\t": "\\t", "\v": "\\v"}
    out = []
    for ch in value:
        code = ord(ch)
        if ch in named:
            out.append(named[ch])
        elif ch.isprintable():
            out.append(ch)
        elif code < 0x80:
            out.append(f"\\x{code:02x}")
        elif code < 0x10000:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def _json_escaped(value: str, html: bool) -> str:
    """The value as it reads inside a JSON string, with or without HTML characters escaped."""
    named = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f",
             " ": "\\u2028", " ": "\\u2029"}
    out = []
    for ch in value:
        if ch in named:
            out.append(named[ch])
        elif ord(ch) < 0x20 or (html and ch in "<>&"):
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def forms(value: str) -> list[str]:
    """The ways a value appears in text, the value first: as written, as `_quoted_escaped` writes it, escaped for a
    URL query or path, inside a JSON string with and without HTML escaping, escaped as markup, with and without its
    quotes and with its `<` alone, as a run's evidence holds it, a value of more than one line with each run of white
    space as one space, as a line folded onto one line holds it, in standard and URL-safe base64 padded and not (each
    also URL escaped, as a Basic login in a logged URL), and cut as a YAML type error quotes a value of more than ten
    bytes, its first seven and three dots. `json.dumps` with its `\\u` escapes and `repr` are forms too; any case
    of each, and the value wrapped across lines, are matched by `Scrubber`."""
    if not value:
        return []
    raw = value.encode()
    found = [_quoted_escaped(value), quote_plus(value, safe="-_.~"), quote(value, safe="$&+:=@-_.~"),
             _json_escaped(value, True), _json_escaped(value, False), json.dumps(value)[1:-1], repr(value)[1:-1],
             html.escape(value), html.escape(value, quote=False), value.replace("<", "&lt;")]
    if "\n" in value or "\r" in value:
        found.append(" ".join(value.split()))
    for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        for b in (encoded.decode(), encoded.decode().rstrip("=")):
            found += [b, quote_plus(b, safe="-_.~"), quote(b, safe="$&+:=@-_.~")]
    out = [value]
    for f in found:
        if f and f not in out:
            out.append(f)
    if len(raw) > 10:
        out.append(raw[:7].decode(errors="ignore") + "...")
    return out


# Characters a text may hide inside a value, as `clean.INVISIBLE` lists them. Those that are white space stay in the
# text and may sit between two characters of a value; the rest are taken out of a text that holds any before it is
# searched a second time, so no pattern carries a class of them for each character of a value.
_SPACE_HIDDEN = "[\\x0b\\x0c\\x1c-\\x1f\\x85\\u2028\\u2029]"
_HIDDEN = re.compile("[\\x00-\\x08\\x0e-\\x1b\\x7f-\\x84\\x86-\\x9f\\u00ad\\u061c\\u180e\\u200b-\\u200f\\u202a-\\u202e"
                     "\\u2060-\\u2064\\u2066-\\u2069\\ufeff\\ufff9-\\ufffb\\U000e0000-\\U000e007f"
                     "\\U000e0100-\\U000e01ef]+")
_BETWEEN = rf"(?:\r?\n[ \t>]*|{_SPACE_HIDDEN})*"  # between two characters of a value: a wrap, or hidden white space
_ESCAPED = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")  # what markdown may write behind a backslash
_STAGES = (8, 64, 512)  # how many parts of a pattern each stage holds; the last stage holds them all
_UNPLAIN = re.compile(r"[\s&\\\x00-\x1f\x7f]")  # what may begin a wrap, white space, an entity, an escape or a hidden character


def _spelled(ch: str) -> str:
    """One character of a value, not white space, as a text may write it: as itself; any but a letter or a digit
    also as a numeric or a named markup entity, and ASCII punctuation behind a markdown backslash. A letter or a digit
    is written as an entity too seldom to pay for the size it adds to the pattern of a long value, such as a key in
    PEM."""
    if ch.isalnum():
        return re.escape(ch)
    code = ord(ch)
    ways = [re.escape(ch), f"&#0*{code};", f"&#x0*{code:x};"]
    if name := codepoint2name.get(code):
        ways.append(f"&{name};")
    if ch == "'":
        ways.append("&apos;")
    if ch in _ESCAPED:
        ways.append(re.escape("\\" + ch))
    return "(?:" + "|".join(ways) + ")"


def _loose(value: str) -> list[str]:
    """A value as the parts of a pattern that also finds it wrapped, with hidden white space inside it, and with any
    of its characters written as `_spelled` writes them: one part for each character, and one for each run of white
    space. A run of its spaces and tabs is as many or more spaces, tabs, no-break spaces or wraps, as word wrap or a
    doubled space leaves it. A value of more than one line is also found folded, as `forms` folds it, when what it
    folds to is no shorter than a value may be: each run of its white space is then one run of any white space, with a
    wrap's quote marks in it. No two parts can match the same character, so a text that misses never makes it
    backtrack far. Other hidden characters are found by `Scrubber`, which searches a text again without them, so each
    of the value's own may be missing."""
    fold = ("\n" in value or "\r" in value) and len(" ".join(value.split())) >= MIN_SCRUB_LENGTH
    out = []
    for part in re.split(r"(\s+)", value):
        if not part:
            continue
        if not part.isspace():
            out += [("" if n == 0 else _BETWEEN) + (f"(?:{_spelled(ch)})?" if _HIDDEN.match(ch) else _spelled(ch))
                    for n, ch in enumerate(part)]
        elif fold:
            out.append(r"\s[\s>]*")
        elif not part.strip(" \t\xa0"):
            out.append(rf"(?:[ \t\xa0]|&nbsp;|\r?\n>*){{{len(part)},}}")
        else:
            out.append(re.escape(part))
    return out


class _Way:
    """One way a value may be written: a form as it is, or the value's loose pattern. It is matched in stages, its
    first 8 parts, then 64, then 512, then all, each compiled only once the stage before it has matched somewhere, so
    a value no text holds costs only its first stage."""

    __slots__ = ("_form", "_parts", "_stages")

    def __init__(self, form: str | None = None, parts: list[str] | None = None):
        self._form, self._parts = form, parts
        self._stages: list[re.Pattern[str] | None] = []

    def __len__(self) -> int:
        return len(self._form) if self._form is not None else len(self._parts or ())

    def source(self, size: int) -> str:
        """The pattern of its first `size` parts."""
        if self._form is not None:
            return re.escape(self._form[:size])
        return "".join((self._parts or [])[:size])

    def end(self, text: str, at: int, begun: bool = False) -> int | None:
        """Where it ends when it is written at `at` in the text, or None. `begun` says its first stage is known to
        match there, so it is not tried unless it is the last."""
        sizes = [n for n in _STAGES if n < len(self)] + [len(self)]
        found = None
        for k, size in enumerate(sizes):
            if begun and k == 0 and len(sizes) > 1:
                continue
            stage = self._stages[k] if k < len(self._stages) else None
            if stage is None:
                stage = re.compile(self.source(size), re.IGNORECASE)
                stages = [*self._stages, *[None] * (k + 1 - len(self._stages))]
                stages[k] = stage
                self._stages = stages  # replaced whole, never changed in place, so threads that share it see each stage
            found = stage.match(text, at)
            if found is None:
                return None
        return found.end() if found is not None else None

# How `_loosely` reads a text: a run of white space, which is a wrap when it holds a line break (with the spaces before
# it, a line's closing backslash, and the next line's indent and quote marks); a run of no-break spaces, which copying
# from a page puts inside a value as it puts a wrap, so it may stand where a wrap may; other white space; a character a
# text hides, such as a zero-width space; or one character.
_TOKEN = re.compile(r"(?P<wrap>(?:[ \t\xa0]*\\?(?:\r\n|\r|\n)[ \t\xa0>]*)+)|(?P<nobreak>[\xa0\u2007\u202f]++(?![^\S\r\n]))"
                    r"|(?P<space>[^\S\r\n]+)|(?P<hidden>[\u00ad\u200b-\u200f\u2060-\u2064\ufeff])|(?P<char>.)",
                    re.DOTALL)
# What a text may write in place of one character, as `_decoded` reads it: a markup entity, an escape of a JSON or
# YAML string or of Python's repr (`\uXXXX`, `\UXXXXXXXX`, `\xXX`, `\n`, `\r`, `\t`), or ASCII punctuation behind a
# markdown backslash.
_ENTITY_OR_ESCAPE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);"
                               r"|\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|[nrt\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e])")
_NAMED_ESCAPE = {"n": "\n", "r": "\r", "t": "\n"}  # a tab written as an escape is read as a break
_NOT_WORD = re.compile(r"[\W_]+")
_NONE, _SPACE, _WRAPPED = 0, 1, 2  # what stands between two characters of a text


def _core(text: str) -> str:
    return _NOT_WORD.sub("", text).lower()


@functools.lru_cache(maxsize=4096)
def _written(token: str) -> str:
    """The character an entity or escape `_ENTITY_OR_ESCAPE` finds stands for, or the token itself when it names no
    one character."""
    if token[0] == "&":
        ch = html.unescape(token)
    elif (kind := token[1]) in "uUx":
        digits = token.removeprefix("\\" + kind)
        ch = chr(code) if (code := int(digits, 16)) <= 0x10FFFF else token
    else:
        ch = _NAMED_ESCAPE.get(token[1], token[1])
    return ch if len(ch) == 1 else token


def _decoded(text: str) -> tuple[str, Callable[[int], tuple[int, int]] | None]:
    """The text with each entity and escape `_ENTITY_OR_ESCAPE` finds written as the character it stands for, as
    `_written` reads it, and where in the text each of its characters is written, or None when the text holds no
    entity or escape and so is its own."""
    out: list[str] = []
    starts: list[int] = []  # where each piece begins in what is read
    pieces: list[tuple[int, int, bool]] = []  # where each piece is written, and whether it is one character's token
    at = size = 0
    for found in _ENTITY_OR_ESCAPE.finditer(text):
        if len(ch := _written(found.group())) != 1:
            continue  # it stays as it is written, part of the text around it
        if found.start() > at:
            starts.append(size)
            pieces.append((at, found.start(), False))
            out.append(text[at:found.start()])
            size += found.start() - at
        starts.append(size)
        pieces.append((found.start(), found.end(), True))
        out.append(ch)
        size += 1
        at = found.end()
    if not pieces:
        return text, None
    if at < len(text):
        starts.append(size)
        pieces.append((at, len(text), False))
        out.append(text[at:])

    last = len(starts) - 1
    k = 0

    def place(n: int) -> tuple[int, int]:  # asked in order, as `_read` asks, so the piece is found by stepping on
        nonlocal k
        if starts[k] > n:
            k = bisect.bisect_right(starts, n) - 1
        while k < last and starts[k + 1] <= n:
            k += 1
        begin, end, whole = pieces[k]
        return (begin, end) if whole else (begin + n - starts[k], begin + n - starts[k] + 1)

    return "".join(out), place


def _read(text: str, decoded: bool) -> tuple[list[str], list[tuple[int, int]], list[int]]:
    """The text's characters lowered, where each is written in the text, and what stands before each: nothing, a run
    of white space, or a wrap. `decoded`, the text is read as `_decoded` writes it, so an entity or an escape is the
    character it writes and a hidden character is passed over, the same however it is written; else each of their
    characters is itself."""
    plain, placed = _decoded(text) if decoded else (text, None)
    chars: list[str] = []
    where: list[tuple[int, int]] = []
    before: list[int] = []
    gap = _NONE
    for found in _TOKEN.finditer(plain):
        kind, token = found.lastgroup, found.group()
        if kind in ("wrap", "nobreak"):
            gap = _WRAPPED
            continue
        if kind == "space":
            gap = max(gap, _SPACE)
            continue
        if kind == "hidden" and decoded:
            continue
        low = token.lower()
        chars.append(low if len(low) == 1 else token)
        where.append(found.span() if placed is None else placed(found.start()))  # each token here is one character
        before.append(gap)
        gap = _NONE
    return chars, where, before


@dataclass(frozen=True)
class _Loose:
    """A value as `_loosely` looks for it: its characters lowered with no white space, and whether white space stands
    before each in the value. Its core, its letters and digits, finds the texts worth reading closely."""

    chars: str
    spaced: tuple[bool, ...]
    core: str

    @classmethod
    def of(cls, value: str) -> _Loose | None:
        chars, spaced, gap = [], [], False
        for ch in value:
            if ch.isspace():
                gap = True
                continue
            low = ch.lower()
            chars.append(low if len(low) == 1 else ch)
            spaced.append(gap and bool(chars[:-1]))
            gap = False
        core = _core(value)
        return cls("".join(chars), tuple(spaced), core) if len(chars) >= MIN_SCRUB_LENGTH and core else None

    def spans(self, chars: str, where: list[tuple[int, int]], before: list[int]) -> Iterator[tuple[int, int]]:
        """Where the text holds the value: a wrap may stand between any two of its characters, and where the value has
        white space the text may have any, a wrap, or none."""
        at = chars.find(self.chars)
        while at >= 0:
            if all(self.spaced[k] or before[at + k] != _SPACE for k in range(1, len(self.chars))):
                yield where[at][0], where[at + len(self.chars) - 1][1]
            at = chars.find(self.chars, at + 1)


def _loosely(text: str, values: tuple[_Loose, ...]) -> list[tuple[int, int]]:
    """Where the text holds a value written loosely: wrapped anywhere, with a line's closing backslash, with spaces
    before a break or a break in place of its own white space, with white space of its own changed or taken out, with
    a character a text hides inside it, with characters written as markup entities, behind a markdown backslash or as
    JSON writes a break or a tab, or any character, a letter or a digit too, written as an entity or as an escape of
    JSON, YAML or Python's repr, in any case. A text whose letters and digits, as written or with each entity and escape
    read as the character it stands for, hold no value's is not read closely."""
    core = _core(text)
    near = tuple(v for v in values if v.core in core)
    if len(near) < len(values) and _ENTITY_OR_ESCAPE.search(text):  # read the escapes only for a value not found
        read = _core(_ENTITY_OR_ESCAPE.sub(lambda found: _written(found.group()), text))
        near += tuple(v for v in values if v not in near and v.core in read)
    if not near:
        return []
    out: list[tuple[int, int]] = []
    for decoded in (False, True):
        chars, where, before = _read(text, decoded)
        joined = "".join(chars)  # one character each, so a place in it is a place in `where`
        for value in near:
            out += value.spans(joined, where, before)
    return out


@dataclass(frozen=True)
class Scrubber:
    """Masks a set of values in text, each in every form, in one pass: at each place where some form begins, the
    longest form found there is masked whole, in any case. The value itself is also masked when a line break, or a
    character a text hides, such as a zero-width space or a soft hyphen, falls inside it, when its white space is
    folded, and when any of its characters is written as a markup entity or behind a markdown backslash, the whole run
    masked. A value shorter than six characters, once trimmed, is left alone, and so is any form of a value that is:
    masking it everywhere would mangle the text around it more than it protects. Its `repr` counts the forms and shows
    none. A value is also masked written loosely, as `_loosely` finds it.

    Building one compiles only the first eight characters of each form into one search; the rest of a form is
    compiled the first time a text holds its start, so a store of many or long values is quick to load."""

    patterns: tuple[str, ...] = field(default=(), repr=False)
    _ways: tuple[_Way, ...] = field(default=(), repr=False, compare=False)
    _starts: tuple[tuple[int, ...], ...] = field(default=(), repr=False, compare=False)  # the ways of each start
    _finder: re.Pattern[str] | None = field(default=None, repr=False, compare=False)
    _written: Mapping[str, tuple[int, ...]] = field(default_factory=dict, repr=False, compare=False)  # by their start
    _wild: tuple[int, ...] = field(default=(), repr=False, compare=False)  # ways whose start is not plain ASCII
    _same: Mapping[int, int] = field(default_factory=dict, repr=False, compare=False)  # loose way: its value as it is
    _probe: list[re.Pattern[str]] = field(default_factory=list, repr=False, compare=False)
    _spaced: tuple[_Loose, ...] = field(default=(), repr=False, compare=False)  # each value as `_loosely` reads it

    @classmethod
    def of(cls, values: Iterable[str]) -> Scrubber:
        trimmed = [t for v in values if len(t := v.strip()) >= MIN_SCRUB_LENGTH]
        kept = {f for v in trimmed for f in forms(v) if len(f) >= MIN_SCRUB_LENGTH}
        ordered = tuple(sorted(kept, key=lambda f: (-len(f), f)))
        loose = sorted(set(trimmed), key=lambda v: (-len(v), v))
        ways = (*(_Way(form=f) for f in ordered), *(_Way(parts=_loose(v)) for v in loose))
        starts: dict[str, list[int]] = {}
        for n, way in enumerate(ways):
            starts.setdefault(way.source(_STAGES[0]), []).append(n)
        finder = re.compile("|".join(starts), re.IGNORECASE) if starts else None
        written: dict[str, list[int]] = {}
        wild: list[int] = []
        for n, start in enumerate([*ordered, *(_HIDDEN.sub("", v) for v in loose)]):
            if (start := start[:_STAGES[0]]).isascii():
                written.setdefault(start.lower(), []).append(n)
            else:
                wild.append(n)
        place = {f: n for n, f in enumerate(ordered)}
        same = {len(ordered) + n: place[v] for n, v in enumerate(loose) if v in place}
        spaced = tuple(v for t in sorted(set(trimmed)) if (v := _Loose.of(t)) is not None)
        return cls(ordered, ways, tuple(map(tuple, starts.values())), finder, {k: tuple(v) for k, v in written.items()},
                   tuple(wild), same, _spaced=spaced)

    def _begun(self, text: str, at: int) -> list[int]:
        """The ways whose first stage matches at `at`, in order, found in one match of every start at once."""
        if not self._probe:
            self._probe.append(re.compile("".join(f"(?:(?=({w.source(_STAGES[0])}))|)"
                                                  for w in (self._ways[s[0]] for s in self._starts)), re.IGNORECASE))
        found = self._probe[0].match(text, at)
        assert found is not None  # every part of it may match nothing
        return sorted(n for k, g in enumerate(found.groups()) if g is not None for n in self._starts[k])

    def _end(self, text: str, at: int, ways: Iterable[int], begun: Container[int] = ()) -> int | None:
        """Where the longest of the ways written at `at` ends, or None when none is. A value's loose way is not tried
        where the value is written as it is, since it would end at the same place."""
        ends: dict[int, int] = {}
        for n in ways:  # in order, so a value as it is comes before its loose way
            if self._same.get(n) in ends:
                continue
            if (e := self._ways[n].end(text, at, n in begun)) is not None:
                ends[n] = e
        return max(ends.values(), default=None)

    def _could(self, text: str, at: int) -> tuple[list[int], set[int]]:
        """Every way that may be written at `at`, and those of them whose first stage is known to match there. Where
        the next eight characters are plain ASCII, with no white space, entity, backslash or hidden character, a way
        can start there only as its own first characters, so it is looked up by them; elsewhere each start is
        tried."""
        window = text[at:at + _STAGES[0]]
        if not window.isascii() or _UNPLAIN.search(window):
            return self._begun(text, at), set()
        found = {n for size in range(MIN_SCRUB_LENGTH, len(window) + 1)
                 for n in self._written.get(window[:size].lower(), ())}
        return sorted(found.union(self._wild)), found

    def _spans(self, text: str) -> list[tuple[int, int]]:
        """Where forms are found, left to right: at each place, the longest of the ways that may be written there,
        those that overlap made one, so one value that begins inside another is masked whole too."""
        out: list[tuple[int, int]] = []
        at = 0
        while self._finder is not None and (hit := self._finder.search(text, at)):
            begin = hit.start()
            end = self._end(text, begin, *self._could(text, begin))
            if end is not None:
                out.append((begin, end))
            at = begin + 1
        return _merged(out)

    def __call__(self, text: str) -> str:
        if self._finder is None:
            return text
        spans = self._spans(text)
        if _HIDDEN.search(text):
            spans = _merged([*spans, *_unhidden(text, self._spans)])
        spans = _merged([*spans, *_loosely(text, self._spaced)])
        if not spans:
            return text
        out, at = [], 0
        for begin, end in spans:
            out += [text[at:begin], MARK]
            at = end
        out.append(text[at:])
        return "".join(out)

    @property
    def longest(self) -> int:
        """The bytes of its longest form, 0 when it masks nothing."""
        return len(self.patterns[0].encode()) if self.patterns else 0

    def __repr__(self) -> str:
        return f"<Scrubber of {len(self.patterns)} forms>"


def _unhidden(text: str, spans: Callable[[str], list[tuple[int, int]]]) -> list[tuple[int, int]]:
    """The spans `spans` finds in the text with its hidden characters taken out, as places in the text itself, each
    holding the hidden characters inside it."""
    kept, where, at, size = [], [], 0, 0  # where[k]: (place in the shorter text, place in the text) of piece k
    for run in _HIDDEN.finditer(text):
        if run.start() > at:
            kept.append(text[at:run.start()])
            where.append((size, at))
            size += run.start() - at
        at = run.end()
    if at < len(text):
        kept.append(text[at:])
        where.append((size, at))
    shorter = [w[0] for w in where]

    def place(n: int) -> int:
        k = bisect.bisect_right(shorter, n) - 1
        return where[k][1] + n - where[k][0]

    return [(place(begin), place(end - 1) + 1) for begin, end in spans("".join(kept))]


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The spans in order, those that overlap made one."""
    out: list[tuple[int, int]] = []
    for begin, end in sorted(spans):
        if out and begin < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((begin, end))
    return out


class Mask:
    """The values read so far, masked in every line handed to it: each secret is added as it is read, so no keychain is
    asked for a value nothing used. A token of the HTTP server is masked too,
    by its shape, so one in a source's record never reaches a model."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._scrubber = Scrubber.of(())

    def add(self, *values: str) -> None:
        fresh = {v for v in values if v and v not in self._values}
        if fresh:
            self._values |= fresh
            self._scrubber = Scrubber.of(self._values)

    def __call__(self, text: str) -> str:
        return self._scrubber(hide_tokens(text))

    @property
    def longest(self) -> int:
        """The bytes of the longest form of a value read so far."""
        return self._scrubber.longest

    def __repr__(self) -> str:
        return f"<Mask of {len(self._values)} values>"


def scrubber(store: Secrets) -> Scrubber:
    """A scrubber for every value the store holds, read once. Build a new one after a secret changes."""
    return Scrubber.of(store.get(name) for name in store.names())


UNMASKED = "the secrets store could not be read, so no secret can be masked, and nothing is sent to a model until it can"


class EveryValue:
    """A mask of every value one store holds, built at its first use and then again only by rule: after `refresh`,
    when this process changed the store, and, for the file store, when the file's change time or size is not what it
    was when the mask was built. So a keychain is read when the mask is built, never on each write.

    A store that cannot be read fails closed: the call raises `SecretsError` with the store's own line, which says
    how to mend it, and the next call reads the store again. A mask is never handed out from a read that failed,
    since an empty or old one would let a value through. So too a file store whose file held a value when the mask
    was built and is gone: an unset leaves an empty file, so a file that is gone may have been lost, and the values
    this process could still meet would go out unmasked."""

    def __init__(self, store: Secrets):
        self.store = store
        self._built: tuple[int, tuple[int, int, int] | None] | None = None
        self._scrubber = Scrubber.of(())
        self._held = False  # the file it was last built from held a value

    def _now(self) -> tuple[int, tuple[int, int, int] | None]:
        stamp = None
        if isinstance(self.store, FileSecrets):
            try:
                st = os.stat(self.store.path)
                stamp = (st.st_ctime_ns, st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = None
        return _GENERATIONS.get(self.store.where(), 0), stamp

    def refresh(self) -> None:
        """Build the mask again before its next use, to take a secret another process added."""
        self._built = None

    def __call__(self) -> Scrubber:
        now = self._now()
        if now != self._built:
            self._built = None
            if self._held and now[1] is None:
                raise SecretsError(f"{UNMASKED}: {self.store.where()} held secrets when this process read it and is "
                                   f"gone: put it back, or start {APP} again to go on without them")
            try:
                self._scrubber = scrubber(self.store)
            except SecretsError as error:
                raise SecretsError(f"{UNMASKED}: {error}") from None
            self._built = now
            self._held = now[1] is not None and bool(self._scrubber.patterns)
        return self._scrubber

    def __repr__(self) -> str:
        return f"<EveryValue of {self.store.where()}>"


def scrub(text: str, *values: str) -> str:
    """The text with each of the values masked, in every form."""
    return Scrubber.of(values)(text)
