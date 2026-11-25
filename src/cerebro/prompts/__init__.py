"""Bundled prompts: the words a run wraps around an agent's own, one markdown file per name.

A prompt is loaded by its name, the file's name without `.md`, so a command can list them and a workspace can eject one
to edit: a file at `prompts/<name>.md` in the workspace's folder takes the place of the bundled one of that name. A
prompt may hold `$name` slots, which `fill` fills. Nothing an agent file or its evidence says is ever read as one.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from string import Template

from ..errors import CerebroError

__all__ = ["PromptError", "fill", "load", "names", "outdated", "unread"]


class PromptError(CerebroError, LookupError):
    """A prompt that is not bundled, or that leaves a slot unfilled."""


def names() -> list[str]:
    """Every bundled prompt's name, in order."""
    return sorted(entry.name.removesuffix(".md") for entry in resources.files(__package__).iterdir()
                  if entry.name.endswith(".md"))


def load(name: str, root: Path | None = None) -> str:
    """The prompt named `name`: the workspace's ejected copy under `root/prompts/` when there is one, else the bundled
    one."""
    if name not in names():  # a name is a bundled prompt's, never a path: an ejected copy takes only its place
        raise PromptError(f"there is no bundled prompt named {name}: use one of {', '.join(names())}")
    if root is not None and (ejected := root / "prompts" / f"{name}.md").is_file():
        return ejected.read_text(encoding="utf-8").strip()
    if name not in names():
        raise PromptError(f"there is no bundled prompt named {name}: use one of {', '.join(names())}")
    return resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()


def fill(name: str, root: Path | None = None, **values: str) -> str:
    """The prompt named `name` with each `$slot` filled from `values`."""
    try:
        return Template(load(name, root)).substitute(values)
    except (KeyError, ValueError) as error:
        raise PromptError(f"prompt {name} has the slot {error} that nothing fills: write $$ for a dollar sign, or "
                          "use only the slots the bundled prompt has") from None


def unread(root: Path) -> list[str]:
    """Each file in `root/prompts/` that no bundled prompt is named after, by its file name, which nothing reads."""
    folder = root / "prompts"
    bundled = set(names())
    return sorted(path.name for path in folder.iterdir() if path.suffix.lower() == ".md" and path.is_file()
                  and (path.suffix != ".md" or path.stem not in bundled)
                  and not _bundled_as(folder, path, bundled)) if folder.is_dir() else []


def _bundled_as(folder: Path, path: Path, bundled: set[str]) -> bool:
    """Whether `path` is the file a bundled prompt's own name opens, as on a disk that ignores case."""
    lower = folder / f"{path.stem.lower()}.md"
    try:
        return path.stem.lower() in bundled and lower.is_file() and path.samefile(lower)
    except OSError:
        return False


def outdated(root: Path) -> list[str]:
    """A line for each file in `root/prompts/` that nothing reads, as `unread` finds them."""
    return [f"prompts/{file} is named after no prompt Cerebro bundles, so nothing reads it: a file there "
            f"takes the place of the bundled prompt of its name, one of {', '.join(names())}: eject one with "
            "cerebro prompts eject <name> and bring your words into it, or move the file aside"
            for file in unread(root)]
