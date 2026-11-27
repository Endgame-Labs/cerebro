"""Creating a workspace, and scaffolding the files in one.

`init` lays down a workspace in a folder. Every workspace gets what lies under the package's `base/`: an `AGENTS.md`
that tells any agent how to work there, and `sources/local.yaml`, a files source over the `local/` folder it makes. It
also gets a `workspace.md`, a `.gitignore` that holds the mirror folder and the state folder, and `settings.yaml`,
written last, so a folder is a workspace only once everything else is in place. A workspace is blank, with no agent,
unless a template is named: `template="gtm"` adds the agents under the package's `templates/gtm/`. It writes nothing
under a folder named for one client, such as `.claude/`. It refuses a folder that is already a workspace, and never
overwrites a file: a file it would write that is already there is refused before anything is written, except
`.gitignore`, which gains the lines it lacks. A link anywhere along a path it would write is refused, and each new file
is created so that a file or link already there is never followed. It stores no key. It says which keys the models need
that the environment does not hold. It trusts the folder it creates, so the workspace's own code runs there, as
`trust` says.

`new_agent` and `new_source` write one file each, from a skeleton that loads as written, and refuse an id the layout
has no place for or a file that is already there. `prompts` lists the bundled prompts, the markdown files under the
package's `prompts/` folder, and `eject_prompt` copies one into the workspace's `prompts/`, whose copy is read from then
on. Nothing here prompts or prints: the CLI asks and prints.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from .agent import FRONT, ID, AgentFileError, load_agent, read_yaml
from .budget import read_caps
from .errors import CerebroError
from .facts import FACTOID_DECISION
from .models import DEFAULTS, ModelSpec, read_models
from .private import FOLDER, make_folder, open_to_others
from .project import check_settings, zone_of
from .shown import shell_path
from .source import EXTRACTS, SourceError, read_source
from .trust import acting, trust

__all__ = [
    "IGNORED",
    "Initialized",
    "NewFile",
    "ScaffoldError",
    "check_domain",
    "check_init",
    "eject_prompt",
    "enclosing_repository",
    "init",
    "mcp_entry",
    "missing_keys",
    "new_agent",
    "new_id",
    "new_source",
    "own_domains",
    "prompts",
    "set_identity",
    "set_model",
    "settings_text",
    "templates",
    "website_domain",
    "workspace_id",
]

BASE = files("cerebro") / "base"  # what every new workspace gets, blank or not
TEMPLATES = files("cerebro") / "templates"  # one folder of agents per template, by its name
PROMPTS = files("cerebro") / "prompts"  # read as files, so no prompt module is imported here
IGNORED = ("articles/", ".cerebro/")  # the mirror folder and the state folder
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
SLUG = re.compile(r"[^A-Za-z0-9_-]+")


class ScaffoldError(CerebroError, ValueError):
    """A workspace or a file cannot be created as asked. The message names the culprit and what to do instead."""


@dataclass
class Initialized:
    """What `init` did: the workspace's folder, each file it wrote relative to it, the lines it added to `.gitignore`,
    the keys a pass needs that the environment lacks, the facts layer's own models' among them, and the MCP entry
    that adds the workspace to a client."""

    root: Path
    written: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    mcp: dict[str, Any] = field(default_factory=dict)
    held: list[str] = field(default_factory=list)  # what acts that the folder held before init, so it is not trusted
    repository: Path | None = None  # the git working tree the workspace was made inside, if any
    open_to_others: bool = False  # the folder was there before init, and other users may read it


def enclosing_repository(root: Path, environ: Mapping[str, str] | None = None) -> Path | None:
    """The top of the git working tree that holds `root`, if a folder above it is one. A workspace that is itself the
    top of a repository is in a folder of its own, so it has none. As git does, the search stops below each folder
    `GIT_CEILING_DIRECTORIES` names."""
    said = (os.environ if environ is None else environ).get("GIT_CEILING_DIRECTORIES", "")
    ceilings = {Path(entry).resolve() for entry in said.split(os.pathsep) if os.path.isabs(entry)}
    for folder in Path(root).resolve().parents:
        if folder in ceilings:
            return None
        if (folder / ".git").exists():  # a folder in a clone, or a file in a worktree or a submodule
            return folder
    return None


@dataclass
class NewFile:
    """A file a scaffold wrote, relative to the workspace, the command to try it with, and lines worth acting on."""

    path: str
    next: str
    notes: list[str] = field(default_factory=list)


# -- init -----------------------------------------------------------------------------------------------------------

DAILY_USD = 50  # the daily cap init writes when no answer gives one


def settings_text(daily_usd: float | None = DAILY_USD, timezone: str | None = None, workspace_id: str | None = None) -> str:
    """The `settings.yaml` init writes: the workspace's id if given, the default models by job, the daily budget, and
    the time zone if given."""
    lines = ["# The models by job, the daily budget, and the time zone. AGENTS.md says what the rest of the folder is."]
    if workspace_id:
        lines.append(f"id: {workspace_id}   # opens this workspace's secrets store, as cerebro secrets set uses it; one id, one "
                     "folder")
    lines.append("models:")
    for job, spec in DEFAULTS.items():
        effort = f", effort: {spec.effort}" if spec.effort else ""
        lines.append(f"  {job}: {{ provider: {spec.provider}, model: {spec.model}{effort} }}")
    if daily_usd is not None:
        amount = int(daily_usd) if float(daily_usd).is_integer() else daily_usd
        lines += ["budget:", f"  daily_usd: {amount}   # model calls stop for the day past this"]
    if timezone:
        lines.append(f"timezone: {timezone}")
    return "\n".join(lines) + "\n"


def _workspace_text(name: str, domains: list[str], org: str = "") -> str:
    listed = ", ".join(domains)
    # under identity, where `project.read_ours` reads them, and the team or organization's name with them
    named = f"\n  name: {org}" if org else ""
    return (f"---\nname: {name}\nidentity:{named}\n  domains: [{listed}]   # your own email domains: people at one are "
            "your side\n---\n\n"
            "Who this workspace is for, in a few lines. Every run reads this file.\n")


PLACEHOLDER = "   # a placeholder from your email's domain: change it to your team or organization's name"
PROFILE_BODY = ("Who you are, in a few lines. Cerebro reads your addresses above to tell your side from everyone "
                "else's.\n")
LOCAL = re.compile(r"[A-Za-z0-9._%+-]+")
LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
TOP = re.compile(r"[a-z]+|xn--[a-z0-9-]*[a-z0-9]")


def check_email(email: str) -> str:
    """An address as init keeps it, its domain in lower case: a local part
    of ASCII letters, digits and . _ % + -, one @, and a domain of two labels or more. Raises ScaffoldError naming
    what is wrong. An empty address is none."""
    email = email.strip()
    if not email:
        return ""
    local, at, domain = email.partition("@")
    if domain and not domain.isascii():  # lowered first, a Kelvin sign would pass as a k
        raise ScaffoldError(f"email {email!r}: write an international domain in its ASCII form, the one starting xn--")
    labels = domain.lower().split(".")
    if not at or "@" in domain or not LOCAL.fullmatch(local) or len(labels) < 2 or not TOP.fullmatch(labels[-1]) \
            or not all(LABEL.fullmatch(label) for label in labels):
        raise ScaffoldError(f"email {email!r}: want one address, such as sam.rivera@quillmoor.example")
    return f"{local}@{domain.lower()}"


_NAMED = {"\n": "a newline", "\r": "a carriage return", "\t": "a tab", "\u2028": "a line separator (U+2028)",
          "\u2029": "a paragraph separator (U+2029)"}


def check_text(what: str, text: str) -> str:
    """One line of text, trimmed: no newline, carriage return, tab, line or paragraph
    separator, or any other control character, U+0000 to U+001F, U+007F and U+0080 to
    U+009F, each refused with what was found."""
    for ch in text:
        found = _NAMED.get(ch) or (f"a control character (U+{ord(ch):04X})" if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                   else "")
        if found:
            raise ScaffoldError(f"{what} {text!r} holds {found}: use one line of text")
    return text.strip()


def website_domain(site: str) -> str:
    """The domain an organization registers for a website, in lower case: with
    or without a scheme, a path, a port or a trailing dot, so www.quillmoor.example and https://shop.quillmoor.example:8443/
    both give quillmoor.example. "" for text that is not one organization's host: a bare word, an IP address, a suffix alone,
    a label of other characters, or a public mail service."""
    import ipaddress

    from .connector import _SECOND_LEVEL, PUBLIC_MAIL, registrable

    site = site.strip()
    if not site or any(ch.isspace() for ch in site):
        return ""
    host = site.split("://", 1)[-1].removeprefix("//").split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rpartition("@")[2]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.lower().strip(".")
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or "" in labels or (len(labels) == 2 and labels[0] in _SECOND_LEVEL) \
            or not all(LABEL.fullmatch(label) for label in labels) or not TOP.fullmatch(labels[-1]):
        return ""
    domain = registrable(host)
    return "" if domain in PUBLIC_MAIL or host in PUBLIC_MAIL else domain


def check_domain(domain: str) -> str:
    """A team or organization's web domain as init keeps it: "" for none, else the
    registrable domain `website_domain` gives. Raises ScaffoldError naming what is wrong: a public mail service, which
    is never one organization's, or text that is not one web domain."""
    from .connector import is_public_mail

    if not domain.strip():
        return ""
    if found := website_domain(domain):
        return found
    if is_public_mail("x@" + domain.strip()):
        raise ScaffoldError(f"domain {domain!r} is a public mail service, not one organization's: give your team or "
                            "organization's web domain, such as quillmoor.example")
    raise ScaffoldError(f"domain {domain!r}: want one web domain, such as quillmoor.example")


def org_guess(email: str) -> tuple[str, str]:
    """What the workspace takes for the team or organization from an address alone: a placeholder
    name made from the address's own domain, and that domain, or nothing for no address or one at a public mail
    service, which says nothing of the team or organization."""
    from .connector import is_public_mail, registrable

    domain = email.strip().rpartition("@")[2].lower() if "@" in email else ""
    if not domain or is_public_mail(email):
        return "", ""
    first = registrable(domain).split(".")[0]
    return first[:1].upper() + first[1:], domain


def _scalar(text: str) -> str:
    """text as a YAML scalar, quoted only where YAML would read it as something else."""
    plain, problem = read_yaml(f"v: {text}")
    return text if not problem and isinstance(plain, dict) and plain.get("v") == text else json.dumps(text)


def _profile_text(name: str, email: str) -> str:
    """profile.md for who you are: your name and address under identity, where the app reads them."""
    return (f"---\nidentity:\n  name: {_scalar(name) if name else chr(34) * 2}\n  email: [{email}]\n---\n\n"
            + PROFILE_BODY)


def templates() -> list[str]:
    """The name of each bundled template, in order."""
    return sorted(item.name for item in TEMPLATES.iterdir() if item.is_dir() and not item.name.startswith(("_", ".")))


def _bundled_files(template: str | None) -> dict[str, Traversable]:
    """The bundled files init copies, by the path each takes in the workspace: the base every workspace gets, then the
    template's agents if one is named. An unknown template is refused with the names there are."""
    if template is not None and template not in templates():
        raise ScaffoldError(f"--template {template!r} is no template: use one of {', '.join(templates())}, or leave "
                            "it out for a blank workspace")
    out: dict[str, Traversable] = {}

    def walk(folder: Traversable, into: str) -> None:
        for item in sorted(folder.iterdir(), key=lambda t: t.name):
            if item.is_dir():
                walk(item, f"{into}/{item.name}" if into else item.name)
            elif not item.name.startswith(".") and item.name != "__init__.py":
                out[f"{into}/{item.name}" if into else item.name] = item

    walk(BASE, "")
    if template is not None:
        walk(TEMPLATES / template, "")
    return out


def workspace_id(root: Path) -> str:
    """A new id for the workspace in `root`: its folder's name as letters, digits and dashes, then six random hex
    digits, so two workspaces whose folders share a name never share an id."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-_").lower()[:48] or "workspace"
    return f"{name}-{secrets.token_hex(3)}"


ID_LINE = re.compile(r"^id:[ \t]*[^\s#]+", re.MULTILINE)


def new_id(root: Path) -> tuple[str, str]:
    """Give the workspace at `root` a fresh id in its settings.yaml, in place of the one there, its comment kept, or as
    a new first line when it has none. Gives the old id, or "", and the new one."""
    path = Path(root) / "settings.yaml"
    text = path.read_text(encoding="utf-8")
    fresh = workspace_id(Path(root).resolve())
    found = ID_LINE.search(text)
    old = found.group(0).split(":", 1)[1].strip() if found else ""
    text = ID_LINE.sub(f"id: {fresh}", text, count=1) if found else f"id: {fresh}\n{text}"
    path.write_text(text, encoding="utf-8")
    return old, fresh


def check_answers(daily_usd: float | None, timezone: str | None) -> None:
    """The budget and the zone checked by the rule the workspace loads them by, before any question,
    so a flag's bad answer is refused before anything is asked. Raises ScaffoldError naming the flag."""
    _settings(daily_usd, timezone)


def _settings(daily_usd: float | None, timezone: str | None, workspace_id: str | None = None) -> str:
    """The settings text, each answer checked by the rule Cerebro loads it by, so a refusal names its flag."""
    for flag, check in (("--daily-usd", lambda: read_caps({"budget": {"daily_usd": daily_usd}})),
                        ("--timezone", lambda: zone_of({"timezone": timezone}))):
        try:
            check()
        except CerebroError as error:
            raise ScaffoldError(f"{flag}: {error}") from None
    text = settings_text(daily_usd, timezone, workspace_id)
    check_settings(read_yaml(text)[0])
    return text


def own_domains(domains: Iterable[str]) -> list[str]:
    """The own domains `--domain` gives, each read by `check_domain`, the rule every other path reads an organization's
    web domain by: a public mail service, a suffix alone or an IP address refused, a website taken, and the
    registrable domain kept. Raises ScaffoldError naming the one that does not do."""
    out = []
    for domain in domains:
        try:
            out.append(check_domain(domain.strip().removeprefix("@")))
        except ScaffoldError as error:
            raise ScaffoldError(f"--domain: {error}") from None
    return list(dict.fromkeys(d for d in out if d))


def _rewrite(path: Path, text: str) -> None:
    """Write a file of the workspace whole, by a partial file renamed into place, keeping its mode."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    partial = path.with_name(f".{path.name}.partial")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), mode)
            out.write(text)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def set_identity(path: Path, key: str, value: str | list[str], comment: str = "", body: str = "") -> None:
    """Set `identity.<key>` in the front matter of a workspace file, such as `email` in `profile.md` or `domains` in
    `workspace.md`, as the app reads them, and keep every other line as it was: the key's line is replaced where
    `identity:` holds it, added under `identity:` where it does not, and `identity:` added at the end of the front
    matter where there is none. A file with no front matter gets one; a missing file is made with the front matter
    and `body` under it. A form this cannot keep, such as `identity:` written on one line, is refused with
    ScaffoldError, and nothing is written."""
    if path.is_symlink():
        raise ScaffoldError(f"{path.name} is a link, so it was not written: write identity.{key} in it by hand")
    shown_value = "[" + ", ".join(value) + "]" if isinstance(value, list) else json.dumps(value, ensure_ascii=False)
    line = f"  {key}: {shown_value}" + (f"   # {comment}" if comment else "")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    found = FRONT.match(text) if text is not None else None
    if found is None:
        front, body = "", ("\n" + text if text else body)
    else:
        front, body = found.group(1), text[found.start(2):] if found.group(2) else ""
    lines = front.splitlines()
    at = next((i for i, row in enumerate(lines) if re.match(r"identity\s*:", row)), None)
    if at is not None and lines[at].split(":", 1)[1].split("#", 1)[0].strip():
        raise ScaffoldError(f"{path.name} writes identity: on one line, so it was not changed: write identity.{key} "
                            "in it by hand")
    if at is None:
        lines += ["identity:", line]
    else:
        end = next((i for i in range(at + 1, len(lines)) if lines[i].strip() and not lines[i][0].isspace()),
                   len(lines))
        mine = next((i for i in range(at + 1, end) if re.match(rf"\s+{re.escape(key)}\s*:", lines[i])), None)
        if mine is None:
            lines.insert(at + 1, line)
        else:
            lines[mine] = line
    after = "---\n" + "\n".join(lines) + "\n---\n" + body
    data, problem = read_yaml("\n".join(lines))
    got = (data.get("identity") or {}).get(key) if isinstance(data, dict) and not problem else None
    if got != value:
        raise ScaffoldError(f"{path.name} would not read identity.{key} back as written, so it was not changed: write "
                            "it by hand")
    before, _ = read_yaml(front) if front else ({}, None)
    if isinstance(before, dict) and {k: v for k, v in before.items() if k != "identity"} != \
            {k: v for k, v in data.items() if k != "identity"}:
        raise ScaffoldError(f"{path.name} would change more than identity.{key}, so it was not changed: write it by hand")
    _rewrite(path, after)


def set_model(root: Path, job: str, spec: ModelSpec) -> None:
    """Set the model of one job in `settings.yaml`, on its line under `models:` as init writes it, `  job: { provider,
    model, effort }`, and keep every other line as it was. A file whose line for the job is in another form is
    refused with ScaffoldError, and nothing is written."""
    path = root / "settings.yaml"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    top = next((i for i, row in enumerate(lines) if re.match(r"models\s*:\s*(#.*)?$", row)), None)
    end = next((i for i in range(top + 1, len(lines)) if lines[i].strip() and not lines[i][0].isspace()),
               len(lines)) if top is not None else 0
    mine = next((i for i in range(top + 1, end) if re.match(rf"\s+{job}\s*:\s*{{.*}}\s*(#.*)?$", lines[i])),
                None) if top is not None else None
    if mine is None:
        raise ScaffoldError(f"settings.yaml writes models.{job} in a form init does not edit, so it was not changed: "
                            f"write it as {job}: {{ provider: {spec.provider}, model: {spec.model} }}")
    effort = f", effort: {spec.effort}" if spec.effort else ""
    indent = lines[mine][:len(lines[mine]) - len(lines[mine].lstrip())]
    note = re.search(r"\}\s*(#.*)$", lines[mine].rstrip("\n"))  # the person's comment on the line stays
    lines[mine] = f"{indent}{job}: {{ provider: {spec.provider}, model: {spec.model}{effort} }}" \
                  f"{'   ' + note.group(1) if note else ''}\n"
    after = "".join(lines)
    data, problem = read_yaml(after)
    if problem or read_models(data)[job] != ModelSpec(spec.provider, spec.model, spec.effort, job):
        raise ScaffoldError(f"settings.yaml would not read models.{job} back as written, so it was not changed")
    _rewrite(path, after)


def _refuse_links(root: Path, rel: str | Path) -> None:
    """Refuse a link anywhere along the path `rel` takes under `root`, dangling or not, so nothing is written through
    one to a place outside the workspace."""
    path = root
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink():
            raise ScaffoldError(f"{path.relative_to(root)} in {root} is a link, and cerebro writes nothing through a "
                                "link: replace it with a plain file or folder, or use another folder")


def check_init(root: Path, *, template: str | None = None) -> Path:
    """The folder init would create the workspace in, made absolute, or a refusal of it before anything is asked or
    written: a folder that is already a workspace, a file where the folder should be, a file init would write that is
    already there, or a link along any path it would write."""
    root = Path(root).resolve()
    if root.exists() and not root.is_dir():
        raise ScaffoldError(f"{root} is a file: name a folder for the workspace")
    if os.path.lexists(root / "settings.yaml"):
        raise ScaffoldError(f"{root} is already a workspace (it holds settings.yaml): run cerebro status --workspace {shell_path(root)}")
    bundled = [*_bundled_files(template), "workspace.md"]
    for rel in [*bundled, ".gitignore", "settings.yaml", "agents", "local"]:
        _refuse_links(root, rel)
    for rel in bundled:
        if os.path.lexists(root / rel):
            raise ScaffoldError(f"{rel} is already in {root}, and init never overwrites a file: move it aside, or "
                                "init another folder")
    return root


def _create(path: Path, data: bytes) -> None:
    """Write a new file, never following or replacing a file or link already there."""
    with path.open("xb") as out:
        out.write(data)


def missing_keys(models: Mapping[str, ModelSpec], held: Mapping[str, str] | Callable[[ModelSpec], str]) -> list[str]:
    """The environment variable of each key the models need that is not held, once each, in job order. `held` is the
    environment, or a function that reads a model's key as the app does: the secrets store, then the environment."""
    read = held if callable(held) else (lambda spec: held.get(spec.variable, ""))
    return list(dict.fromkeys(spec.variable for spec in models.values() if not read(spec).strip()))


def mcp_entry(root: Path, command: str) -> dict[str, Any]:
    """The entry that adds the workspace to an MCP client: a server name from the folder's name, every run of other
    characters and of hyphens made one hyphen, the command, and its arguments. The
    guided init writes it into a client's config where the person says so."""
    slug = SLUG.sub("-", Path(root).name).strip("-") or "workspace"
    return {"name": re.sub(r"-{2,}", "-", f"cerebro-{slug}"), "command": command, "args": ["mcp", "--workspace", str(Path(root).resolve())]}


def _gitignore(path: Path) -> list[str]:
    """Add the lines of `IGNORED` that `.gitignore` lacks, and give them."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)  # a link here, even one that dangles, is refused, never followed
    with os.fdopen(fd, "r+b") as file:
        text = file.read().decode("utf-8")
        have = {line.strip() for line in text.splitlines()}
        added = [line for line in IGNORED if line not in have and line.rstrip("/") not in have]
        if added:
            lead = "" if not text or text.endswith("\n") else "\n"
            file.write((lead + "\n".join(added) + "\n").encode("utf-8"))
    return added


def init(root: Path, *, template: str | None = None, daily_usd: float | None = DAILY_USD, timezone: str | None = None,
         domains: Iterable[str] = (), environ: Mapping[str, str] | None = None,
         command: str = "cerebro", name: str = "", email: str = "", org: str = "") -> Initialized:
    """Create a workspace in `root`, creating the folder if needed. On a failure part way, what it wrote is removed.

    `name` and `email` are who you are, written to `profile.md` when either is given. The own domains go under
    `identity:` in `workspace.md`: those given, else the address's own domain, unless it is at a public mail service.
    `org` names the team or organization in `workspace.md`; with none, an address's domain gives a placeholder,
    marked as one, and with no address the folder's name stands."""
    root = check_init(root, template=template)
    name, org, email = check_text("name", name), check_text("team or organization", org), check_email(email)
    if (name or email) and os.path.lexists(root / "profile.md"):
        raise ScaffoldError(f"profile.md is already in {root}, and init never overwrites a file: move it aside, or init "
                            "another folder")
    settings = _settings(daily_usd, timezone, workspace_id(root))
    guessed, guess_domain = org_guess(email)
    own = own_domains(domains) or ([guess_domain] if guess_domain else [])
    named = _scalar(org) if org else _scalar(guessed) + PLACEHOLDER if guessed else ""
    workspace = _workspace_text(root.name, own, named)
    done = Initialized(root, held=acting(root), repository=enclosing_repository(root, environ))
    made: list[Path] = []  # every file and folder init created, to remove on a failure
    ignore = root / ".gitignore"
    kept = ignore.read_bytes() if ignore.is_file() and not ignore.is_symlink() else None  # put back on a failure
    try:
        if not root.exists():
            made.append(root)
            make_folder(root)
        else:
            done.open_to_others = open_to_others(root)
        for folder in [root / "agents", root / "local"]:
            _refuse_links(root, folder.name)
            if not folder.exists():
                made.append(folder)
                folder.mkdir(mode=FOLDER)
        files = [*((rel, source.read_bytes()) for rel, source in _bundled_files(template).items()),
                 ("workspace.md", workspace.encode("utf-8")),
                 *([("profile.md", _profile_text(name, email).encode("utf-8"))] if name or email else [])]
        for rel, data in files:
            path = root / rel
            _refuse_links(root, rel)
            for parent in reversed(path.relative_to(root).parents[:-1]):
                if not (root / parent).exists():
                    made.append(root / parent)
                    (root / parent).mkdir(mode=FOLDER)
            _create(path, data)
            made.append(path)
            done.written.append(rel)
        _refuse_links(root, ".gitignore")
        if kept is None:
            made.append(ignore)
        done.ignored = _gitignore(ignore)
        _refuse_links(root, "settings.yaml")
        _create(root / "settings.yaml", settings.encode("utf-8"))
        made.append(root / "settings.yaml")
        done.written.append("settings.yaml")
    except (OSError, ScaffoldError) as error:
        for path in reversed(made):
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() and not path.is_symlink() \
                else path.unlink(missing_ok=True)
        if kept is not None:
            ignore.write_bytes(kept)
        if isinstance(error, ScaffoldError):
            raise
        raise ScaffoldError(f"init could not write in {root} ({error.strerror or error}): check the folder can be "
                            "written, then run init again") from None
    if not done.held:
        trust(root)  # init trusts only what it made: a folder that held anything that acts waits for cerebro trust
    settings_map, _ = read_yaml(settings)
    needed = {**read_models(settings_map), "facts": FACTOID_DECISION}  # a new workspace extracts through Factoid
    done.missing_keys = missing_keys(needed, {} if environ is None else environ)
    done.mcp = mcp_entry(root, command)
    return done


# -- agents new and sources new ------------------------------------------------------------------------------------

PROMPT = """
# Instructions

Say what this agent writes, for whom, and from what. Every sentence cites a
fact. If facts disagree, say so and cite both. Leave out anything you cannot
cite.
"""
OUTPUT = "\n# Output\n\n## Summary\n"
FACTS = "evidence:\n  - facts: { window: last_90_days }   # the article's facts from the last 90 days\n"
MANUAL = "heartbeat: [manual]   # try it with agents run, then set [change] once it reads well\n"
SUBAGENTS_HINT = ("# subagents: [overview]   # files under subagents/ beside this one, each returns one part; "
                  "leave it out to write the whole article\n")
CURATOR = """---
authority: {{ admit: false, archive: false, merge: false, split: false }}   # what this curator does on its own; a kind set false, or left out, waits for you in cerebro review
evidence:
  - query: {{ from: facts, window: last_90_days }}   # what the sources say, to admit from
  - held: {{}}   # the records your sources hold for {collection}, newest first
heartbeat: [manual]   # try it with agents run, then set [change, daily] once it reads well
---

# Instructions

Decide which articles {collection} holds. Read what your evidence says, ask
the workspace's sources with fetch where one answers questions, admit what belongs
with a one-line reason, and archive or merge where one is called for. Admit at most 25 a run. When a name has several candidates and nothing
gives a clear winner, say so in your last line instead of guessing.
"""


def _workspace(root: Path) -> Path:
    root = Path(root).resolve()
    if not (root / "settings.yaml").is_file():
        raise ScaffoldError(f"{root} is not a workspace (no settings.yaml in it): run in the workspace's folder, or name it "
                            "with --workspace")
    return root


def _agent_text(id: str) -> str:
    collection, _, name = id.partition("/")
    if name == "curator":
        return CURATOR.format(collection=collection)
    if name.startswith("subagents/"):
        return f"---\n{MANUAL}---\n{PROMPT}"
    hint = SUBAGENTS_HINT if name == "article" else ""
    return f"---\n{hint}{FACTS}{MANUAL}---\n{PROMPT}{OUTPUT}"


def _write_new(path: Path, text: str, label: str, listing: str) -> None:
    _refuse_links(path.parents[label.count("/")], label)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as out:
            out.write(text)
    except FileExistsError:
        raise ScaffoldError(f"{label} already exists: run {listing}, or pick another name") from None


def _listed_by(root: Path, id: str) -> list[str]:
    """For a subagent, a line that says where to list it, unless its composer lists it already."""
    collection, name = id.split("/")[0], id.split("/")[-1]
    composer = root / "agents" / collection / "article.md"
    try:
        listed = load_agent(composer, root / "agents").subagents if composer.is_file() else None
    except AgentFileError:
        listed = None
    if listed is not None and name in listed:
        return []
    return [f"list {name} under subagents: in agents/{collection}/article.md, or it does not run"]


def new_agent(root: Path, id: str) -> NewFile:
    """Write `agents/<id>.md` from the skeleton its role takes, and check it loads."""
    root = _workspace(root)
    if not ID.fullmatch(id):
        raise ScaffoldError(f"agent id {id!r} is not lower case words joined by - and /: write one such as "
                            "reports/weekly")
    if "/" not in id:
        raise ScaffoldError(f"{id} has no folder, and every agent belongs to a collection: try {id}/curator to decide "
                            f"which articles it holds, {id}/article to write each of them, or {id}/<name> for one")
    label = f"agents/{id}.md"
    path = root / label
    _write_new(path, _agent_text(id), label, "cerebro agents list")
    try:
        load_agent(path, root / "agents")
    except AgentFileError as error:
        path.unlink()
        raise ScaffoldError(f"{label} has no place in the layout ({error}): pick another id") from None
    notes = _listed_by(root, id) if id.count("/") == 2 else []
    collection, _, name = id.partition("/")
    if name.startswith("subagents/"):  # a subagent runs inside the agent that composes it, never on its own
        composer = f"{collection}/article"
        then = (f"cerebro agents run {composer}, which runs it" if (root / "agents" / f"{composer}.md").is_file()
                else "list it under subagents: in the file of the agent that composes it, and run that agent")
        return NewFile(label, f"fill in {label}, then {then}", notes)
    return NewFile(label, f"fill in {label}, then cerebro agents run {id}", notes)


def new_source(root: Path, name: str, globs: Iterable[str] = (), extract: str = "facts") -> NewFile:
    """Write `sources/<name>.yaml`, a folder of files read one record per file, and check it reads. With no glob it
    reads `<name>/**/*.md`."""
    root = _workspace(root)
    if not NAME.fullmatch(name):
        raise ScaffoldError(f"source name {name!r} is not lower case words joined by -: write one such as "
                            "meeting-notes")
    if extract not in EXTRACTS:
        raise ScaffoldError(f"--extract {extract!r}: write one of {', '.join(EXTRACTS)}")
    patterns = list(globs) or [f"{name}/**/*.md"]
    lines = ["# Local files, one record per file, its body the text. A relative glob is relative to the workspace.",
             "kind: files", "select:"]
    for pattern in patterns:
        lines += [f"  - glob: {_quoted(pattern)}", f"    extract: {extract}"]
    label = f"sources/{name}.yaml"
    path = root / label
    _write_new(path, "\n".join(lines) + "\n", label, "cerebro status")
    try:
        read_source(path)
    except SourceError as error:
        path.unlink()
        raise ScaffoldError(f"{label} would not read ({error}): give --glob again") from None
    folder = patterns[0].split("/")[0] if not globs else ""
    if folder and not (root / folder).exists():
        (root / folder).mkdir()
    return NewFile(label, f"put files in {folder or 'the folders the globs name'}, then cerebro serve --once")


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- prompts list and prompts eject ---------------------------------------------------------------------------------

def _bundled() -> dict[str, Traversable]:
    if not PROMPTS.is_dir():
        return {}
    return {item.name.removesuffix(".md"): item for item in sorted(PROMPTS.iterdir(), key=lambda t: t.name)
            if item.name.endswith(".md") and item.is_file()}


def prompts(root: Path) -> list[dict[str, Any]]:
    """Each bundled prompt by name, and whether the workspace at `root` has ejected its own copy, which runs read in its
    place."""
    root = _workspace(root)
    return [{"name": name, "ejected": (root / "prompts" / f"{name}.md").is_file(), "path": f"prompts/{name}.md"}
            for name in _bundled()]


def eject_prompt(root: Path, name: str) -> NewFile:
    """Copy the bundled prompt `name` into `prompts/<name>.md`, to edit. It refuses a copy that is already there."""
    root = _workspace(root)
    bundled = _bundled()
    if name not in bundled:
        known = f"use one of {', '.join(bundled)}" if bundled else "this version bundles none"
        raise ScaffoldError(f"there is no bundled prompt named {name}: {known}")
    label = f"prompts/{name}.md"
    _write_new(root / label, bundled[name].read_text(encoding="utf-8"), label, "cerebro prompts list")
    return NewFile(label, f"edit {label}: every run reads it in place of the bundled one, and deleting it puts the "
                          "bundled one back")
