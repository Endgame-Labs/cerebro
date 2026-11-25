"""What Cerebro keeps is readable by its owner alone: the state folder `.cerebro/`, the store's file wherever
settings put it with the files SQLite makes beside it, Factoid's data, and the mirrored `articles/`. Folders are
0700 and files 0600.

A file Cerebro makes is created with that mode. SQLite gives its `-wal`, `-shm` and journal files the mode of the
database file, so a store file made 0600 keeps them so. A library that opens its own files is opened inside
`owner_only`, under a creation mask of 077. A workspace whose modes are wider is tightened on open by `tighten`, which only
ever takes bits away. Windows has no such modes, so on Windows nothing here changes a mode.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .shown import shell_path, shown_path

FOLDER = 0o700
FILE = 0o600
OTHERS = 0o077
POSIX = os.name != "nt"
SIDE_FILES = ("-wal", "-shm", "-journal")

__all__ = [
    "FILE",
    "FOLDER",
    "POSIX",
    "SIDE_FILES",
    "make_file",
    "make_folder",
    "open_to_others",
    "owner_only",
    "tighten",
    "write_file",
]


def make_folder(path: Path) -> None:
    """Make `path` and each folder above it that is missing, each 0700. A folder already there is left as it is."""
    missing = []
    at = Path(path)
    while not at.exists() and not at.is_symlink() and at != at.parent:
        missing.append(at)
        at = at.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=FOLDER)
        except FileExistsError:
            continue
        if POSIX:
            os.chmod(folder, FOLDER)  # mkdir's mode passes through the creation mask, which may take more


def make_file(path: Path) -> None:
    """Make an empty file at `path`, 0600, unless something is there."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), FILE)
    except FileExistsError:
        return
    try:
        if POSIX:
            os.fchmod(fd, FILE)
    finally:
        os.close(fd)


def write_file(path: Path, text: str) -> None:
    """Write `text` to `path`, 0600, whatever mode a file already there had."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), FILE)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        if POSIX:
            os.fchmod(fd, FILE)
        out.write(text)


@contextmanager
def owner_only() -> Iterator[None]:
    """Run the block under a creation mask of 077, so what a library creates in it is the owner's alone. The mask is
    the process's, so a file another thread makes meanwhile is the owner's alone too, never wider."""
    if not POSIX:
        yield
        return
    old = os.umask(OTHERS)
    try:
        yield
    finally:
        os.umask(old)


def open_to_others(path: Path) -> bool:
    """Whether users other than the owner have any access to `path`."""
    return POSIX and bool(os.stat(path).st_mode & OTHERS)


def tighten(paths: Iterable[Path], root: Path) -> list[str]:
    """Take every bit for other users off each of `paths` that exists, and off all under it, links aside. Gives at
    most one line saying what was tightened and one saying what could not be, each path shown relative to `root`
    when under it."""
    if not POSIX:
        return []
    changed: list[Path] = []
    failed: list[tuple[Path, OSError]] = []
    for top in paths:
        hit = False
        for path in _walk(Path(top)):
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue
            if stat.S_ISLNK(mode) or not mode & OTHERS:
                continue
            try:
                os.chmod(path, stat.S_IMODE(mode) & ~OTHERS)
                hit = True
            except OSError as error:
                failed.append((path, error))
        if hit:
            changed.append(Path(top))
    said = []
    if changed:
        said.append(f"modes: {', '.join(_named(p, root) for p in changed)} could be read by other accounts on this "
                    "machine, so cerebro made them readable by you alone")
    if failed:
        path, error = failed[0]
        said.append(f"modes: {_named(path, root)} can be read by other accounts on this machine, and cerebro could "
                    f"not change that ({error.strerror or error}): run chmod -R go-rwx {shell_path(path)}")
    return said


def _walk(top: Path) -> Iterator[Path]:
    if not top.exists() or top.is_symlink():
        return
    yield top
    if top.is_dir():
        for folder, dirs, files in os.walk(top):
            for name in [*dirs, *files]:
                yield Path(folder) / name


def _named(path: Path, root: Path) -> str:
    try:
        return shown_path(path.relative_to(root))
    except ValueError:
        return shown_path(path)
