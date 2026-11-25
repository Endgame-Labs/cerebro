"""What a workspace keeps outside its folder, found and removed: `cerebro forget`.

A workspace keeps three things outside its folder: its secrets, in the keychain or the file store, keyed by the `id:` in
its `settings.yaml`; its claim on that id in the secrets folder's `.folders.json`; and its trust record in
`trusted.json`. `of_workspace` finds them for a workspace that is there, and `left_behind` finds what workspaces whose
folders are gone left. Nothing here loads a workspace or opens its store: the id is the one value read from its
settings, so a workspace its owner no longer trusts can be forgotten. A secret is named, never its value.

The rule that keeps a copy safe: while another folder the records know still holds the same id, the secrets stay,
since they are that folder's too. A folder that moved looks the same as one that was deleted, so the command removes
what a gone folder left only on a yes typed at a terminal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import secrets as secrets_
from . import trust as trusting

__all__ = ["Outside", "claims_file", "left_behind", "of_workspace", "remove", "there"]


@dataclass
class Outside:
    """What one workspace keeps outside its folder, that `remove` would remove. `folder` is "" when no record says where
    the workspace was, and `id` "" when no record names its id."""

    folder: str = ""
    id: str = ""
    secrets: list[tuple[str, list[str]]] = field(default_factory=list)  # each place, with the names it holds
    unread: list[str] = field(default_factory=list)  # a line for each store that cannot be read here
    claim: str = ""  # the folder the claim on the id names, when the claim goes
    trusted: bool = False  # a trust record of the folder
    others: list[str] = field(default_factory=list)  # other folders that may hold the id: its secrets stay

    def empty(self) -> bool:
        return not (self.secrets or self.claim or self.trusted)


def there(path: str | Path) -> bool:
    """Whether anything is at `path`. Anything but plainly gone counts as there, so nothing goes on a doubt."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _same(a: str, b: str) -> bool:
    if os.path.normpath(a) == os.path.normpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _holders(workspace_id: str, folder: str) -> list[str]:
    """The folders other than `folder` that the claims and trust records know and that may hold `workspace_id`."""
    known = [secrets_.claims().get(workspace_id, "")] + [r["path"] for r in trusting.records()]
    return sorted({p for p in known if p and not (folder and _same(p, folder)) and secrets_.may_hold(Path(p), workspace_id)})


def _fill(out: Outside) -> Outside:
    """Add the secrets and the claim of `out.id`, unless another folder holds the id."""
    if not out.id:
        return out
    out.others = _holders(out.id, out.folder)
    if not out.others:
        out.secrets, out.unread = secrets_.held(out.id)
    claimed = secrets_.claims().get(out.id, "")
    if claimed and (out.folder and _same(claimed, out.folder) or not secrets_.may_hold(Path(claimed), out.id)):
        out.claim = claimed
    return out


def _valid_id(found: str) -> str:
    try:
        return secrets_._check_id(found)
    except secrets_.SecretsError:
        return ""  # no store can be keyed by it, so none holds anything under it


def of_workspace(root: Path) -> Outside:
    """What the workspace at `root` keeps outside its folder. Its settings are read for the id alone."""
    real = str(Path(root).resolve())
    out = Outside(folder=real, id=_valid_id(trusting.settings_id(Path(real))))
    out.trusted = any(r["path"] == real for r in trusting.records())
    return _fill(out)


def left_behind() -> list[Outside]:
    """What workspaces no longer at their recorded folder left behind: a claim whose folder no longer holds its id, a
    trust record whose folder is gone, and secrets whose id no folder holds, folders first, then by id."""
    claims = secrets_.claims()
    found: list[Outside] = []
    for workspace_id, folder in sorted(claims.items()):
        if _valid_id(workspace_id) and not secrets_.may_hold(Path(folder), workspace_id):
            found.append(Outside(folder=folder, id=workspace_id))
    for record in trusting.records():
        if there(record["path"]):
            continue
        match = next((o for o in found if o.folder == record["path"]), None)
        if match is None:
            match = Outside(folder=record["path"])
            found.append(match)
        match.trusted = True
    living = {i for i, folder in claims.items() if secrets_.may_hold(Path(folder), i)}
    for workspace_id in sorted(set(secrets_.stored_ids()) | set(claims)):
        if workspace_id in living or not _valid_id(workspace_id) or any(o.id == workspace_id for o in found):
            continue
        found.append(Outside(id=workspace_id))
    for out in found:
        _fill(out)
    return sorted((o for o in found if not o.empty()), key=lambda o: (o.folder == "", o.folder, o.id))


def remove(out: Outside) -> list[str]:
    """Remove what `out` holds, and give a line for each thing removed, by name. Each step can run again."""
    lines = []
    if out.secrets:
        for where, gone in secrets_.forget_each(out.id):
            lines.append(f"removed  secrets {', '.join(gone)} from {where}" if gone else
                         f"removed  the list of secret names of id {out.id} from {where}")
    if out.claim and secrets_.release(out.id):
        lines.append(f"removed  the claim on id {out.id}, from {claims_file()}")
    if out.trusted and trusting.untrust(out.folder):
        lines.append(f"removed  the trust record of {out.folder}, from {trusting.trusted_path()}")
    return lines


def claims_file() -> Path:
    """The file that records which folder claims each id."""
    return secrets_.secrets_dir() / secrets_.FOLDERS
