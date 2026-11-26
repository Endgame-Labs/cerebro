"""The command line. It builds the app object, calls it, and prints. It holds no logic of its own.

Every command takes `--workspace`, the workspace's folder, and the current folder without it. None looks in a folder above.
Reads take `--json` and print the app object's data. A refusal is one line, and the exit status is 1. `init` asks
only when its input is a terminal, and every answer has a flag.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import click
from click.shell_completion import (
    BashComplete,
    CompletionItem,
    FishComplete,
    ShellComplete,
    ZshComplete,
    add_completion_class,
)

from . import clock, preview, scaffold
from . import review as reviewing
from . import trust as trusting
from .agent import Agent, refusal_of, refused_ids
from .app import Cerebro, PassResult
from .budget import BudgetSpent
from .caller import KIND_WORDS, Caller
from .compile import Compiled, left_alone, made_safe, out_of_working_set, targets
from .connector import one_line
from .errors import CerebroError
from .forget import Outside, claims_file, left_behind, of_workspace, remove, there
from .models import PROVIDERS
from .run import WROTE_NOTHING
from .runner import (
    _shown,
    failing,
    failure,
    held_runs,
    left_by_hand,
    let_go,
    ran_by_hand,
    run_forever,
    waiting_runs,
    waits_by_hand,
)
from .shown import LINE_CAP, shell_path, shown, shown_json, shown_path

__all__ = ["main", "open_workspace"]

YOU = Caller("you", "person")
INTERVAL = 30  # seconds between passes under serve, unless settings.yaml says serve: { interval }
JOIN = 8.0  # seconds from a stop's signal to the end of its wait for the pass under way: what is left of a supervisor's 10 ends it


def open_workspace(root: Path | None) -> Cerebro:
    """The app object over the workspace at `root`. A test swaps this to hand the app its stand-ins."""
    return Cerebro(root)


class _Whole(click.ClickException):
    """A refusal that names a folder, printed whole, any part from a workspace's files capped where it is put in."""


class _Usage(CerebroError):
    """A refusal a person answers by naming the thing again, more exactly: exit 2."""


class _Waits(click.ClickException):
    """What waits on a person off a terminal, printed as a refusal is, and exit 2, as `serve --once` exits for its
    first run's list."""

    exit_code = 2


def _terminal_out() -> bool:
    return sys.stdout.isatty()


def _failed_line(run: str, plain: bool = False) -> str:
    """The last line for a usage error, `[x] Failed. Run: <run>`, styled where stderr takes styles."""
    if plain or not sys.stderr.isatty():  # the printer is loaded only where it draws
        return f"[x] Failed. Run: {run}"
    from .output import Printer

    return Printer().last_line(failed=True, run=run)


class _WaitsToRun(_Waits):
    """A connect that waits on a person off a terminal: its refusal, then the failed line with the command to run,
    as a usage error ends."""

    def __init__(self, message: str, run: str):
        super().__init__(message)
        self.run = run

    def show(self, file: Any = None) -> None:
        super().show(file)
        if self.run:
            click.echo(_failed_line(shown(self.run, 300)), err=True)


def _checked_workspace(root: Path | None, trust: bool = True) -> None:
    """Refuse a command on a workspace whose settings.yaml does not check,
    and, unless `trust` is False, one its owner has not trusted, as loading one is refused, though this one loads
    none."""
    from .project import checked_settings, read_settings

    target = (root or Path.cwd()).resolve()
    if (target / "settings.yaml").is_file():
        if trust:
            read_settings(target)
        else:
            checked_settings(target)


def _root_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    return click.option("--workspace", "root", type=click.Path(path_type=Path, file_okay=False), default=None,
                        callback=_named_before, help="The workspace folder. The default is the current folder.")(fn)


def _named_before(ctx: click.Context, _param: click.Parameter, value: Path | None) -> Path | None:
    """The workspace --workspace names here, kept for the commands after this one on the line, else the one it named
    before, so the last given wins wherever it stands."""
    if value is not None:
        ctx.meta[WORKSPACE] = value
    return value if value is not None else ctx.meta.get(WORKSPACE)


def _json_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    return click.option("--json", "as_json", is_flag=True, help="Print the data as JSON.")(fn)


def _refusing(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Print a refusal the app raises as its one line, and exit 1, or 2 for what waits on a person off a terminal."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except trusting.UntrustedWorkspace as error:
            raise click.ClickException(shown_path(error)) from None
        except reviewing.NotATerminal as error:  # masked before its whitespace folds, so a value's own runs match
            raise _Waits(shown(" ".join(_every_value(kwargs.get("root"))(str(error)).split()))) from None
        except (CerebroError, NotImplementedError) as error:
            said = " ".join(_every_value(kwargs.get("root"))(str(error)).split())
            raise click.ClickException(shown(_or_init(said, kwargs.get("root")))
                                       or "refused, with no reason given") from None
        except click.ClickException as error:
            error.message = shown(_every_value(kwargs.get("root"))(error.message),
                                  None if isinstance(error, _Whole) else LINE_CAP)
            raise

    return run


def _masking(lines: list[str], root: Path | None) -> Callable[[str], str]:
    """`_every_value` for the refused lines of a file that did not load, read only when there is one to print."""
    return _every_value(root) if lines else (lambda text: text)


def _every_value(root: Path | None) -> Callable[[str], str]:
    """A mask of every value the secrets store of the workspace at `root`, or the current folder, holds, read for a
    refusal, which may quote one though the workspace did not open: by the id its settings name, from every store a
    value of it can be in, and nothing written. When none can be read, text stays as it is: a refusal never fails for
    want of a store."""
    try:
        import yaml

        from .secrets import every_store, scrubber

        folder = Path(root) if root is not None else Path.cwd()
        settings = yaml.safe_load((folder / "settings.yaml").read_text(encoding="utf-8").removeprefix("\ufeff"))
        workspace_id = settings.get("id") if isinstance(settings, dict) else None
        stores = every_store(workspace_id)[0] if isinstance(workspace_id, str) else []
    except Exception:  # noqa: BLE001 - no store to read: the refusal is printed as it is
        stores = []
    scrubbers = []
    for store in stores:
        with contextlib.suppress(Exception):  # a store that cannot be read masks nothing, and the others still do
            scrubbers.append(scrubber(store))
    return lambda text: functools.reduce(lambda said, scrub: scrub(said), scrubbers, text)


NOT_A_WORKSPACE = "is not a workspace (no settings.yaml in it)"


def _or_init(said: str, root: Path | None) -> str:
    """A refusal that the folder is no workspace, with the command that makes one there, which
    names init: with the folder --workspace named, or bare in the current folder. Not in a folder inside a
    workspace, where init would make a second one inside it."""
    target = (root or Path.cwd()).resolve()
    if NOT_A_WORKSPACE not in said or any((folder / "settings.yaml").is_file() for folder in (target, *target.parents)):
        return said
    return f"{said}, or make one there with cerebro init" + (f" {shell_path(root)}" if root is not None else "")


def _data(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def _print_json(value: Any) -> None:
    click.echo(shown_json(json.dumps(_data(value), indent=2, default=str, ensure_ascii=False)))


def _echo(message: Any = "", err: bool = False, cap: int | None = LINE_CAP) -> None:
    """Print a line, with any control character in it, which a workspace's files may hold, shown as an escape. A line
    that names a folder passes `cap=None`, and caps any part from a workspace's files itself."""
    click.echo(shown(message, cap), err=err)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _masked(result: PassResult, root: Path | None) -> PassResult:
    """`result`, each refused line masked with every value the store of the workspace at `root` holds, since a refusal
    quotes the file it refuses."""
    result.refused = list(map(_masking(result.refused, root), result.refused))
    return result


def _print(result: PassResult, said: set[str] | None = None, agents: bool = True) -> None:
    """Print what a pass did. With `said`, a refusal or a notice printed before, such as what Cerebro said when it
    loaded, is not printed again, so `serve` says each once. A pass that ran no agent, as `distill`, says nothing of
    them."""
    for kind, lines in (("refused  ", result.refused), ("notice   ", result.notices)):
        for line in lines:
            if said is None or line not in said:
                _echo(f"{kind} {line}")
            if said is not None:
                said.add(line)
    for source, count in result.listed.items():
        _echo(f"listed    {source}: {_count(count, 'record')}")
    if result.read or result.extracted:
        _echo(f"facts     {_count(result.read, 'record')} read, {_count(result.extracted, 'fact')} extracted")
    for agent, why in result.due.items():
        _echo(f"due       {agent}: {why}")
    if agents and not any(row for row in (result.due, result.skipped, result.failed, result.ran)) and not result.runs \
            and not refused_ids(result.refused):
        _echo("agents    none: write one with cerebro agents new <collection>/<name>")
    for article in result.compiled:
        _echo(f"wrote     {article}  articles/{article}.md")
    if result.grounding:  # what grounding did on each article, as counts: each line of it is kept on its run's line
        from .ground import counted

        for key, tally in result.grounding.items():
            if said := counted(tally):
                _echo(f"grounding {key}: {said}")
    for key, n in result.cleaned.items():  # each thing it made safe is kept on its run's line
        _echo(f"cleaned   {key}: {made_safe(n)}")
    for agent, why in result.skipped.items():
        _echo(f"skipped   {agent}: {why}")
    for agent, why in result.held.items():
        _echo(f"held      {agent}: {why}")
    for line in result.warnings:  # what grounding repaired or dropped is kept on its run's line
        if line not in result.grounded:
            _echo(f"warning   {line}")
    for agent, why in result.ran.items():  # no failure: it runs again once what it reads moves
        _echo(f"ran       {agent}: wrote nothing: {why}")
    for agent, why in result.failed.items():
        _echo(f"failed    {agent}: {why}")
    for line in result.stops:  # a daily cap reached kept work from starting: it waits, and none of it failed
        if said is None or line not in said:  # serve says a stop once, until it changes
            _echo(line)
    _echo(f"Done: {len(result.compiled)} written, "
          + (f"{len(result.ran)} ran and wrote nothing, " if result.ran else "") + f"{len(result.failed)} failed"
          + (f", {len(result.held)} held for the next pass." if result.held else "."))
    if result.next:  # a first run that failed under serve: what to run, as serve --once ends on it
        _echo(f"next      {result.next}")


def _idle(workspace: Cerebro, result: PassResult) -> bool:
    """Whether a daily cap stopped the pass with nothing done, so the command exits 1:
    nothing landed, and no model call or run was paid for."""
    return bool(result.stops) and not (result.compiled or result.extracted or result.retracted or workspace.ledger.recorded)


def _passes(workspace: Cerebro) -> Callable[[PassResult], None]:
    """What `serve` prints of each pass: each line once where `_print` says so, its stop lines when they differ from
    the last it said, in their words or the crossing they name, and, once the caps that stopped it are no longer
    reached, that model calls resume."""
    said: set[str] = set()
    last: list[Any] = []  # the stop lines said last, the crossings they name, and the caps they were said under

    def printed(result: PassResult) -> None:
        if last and not result.stops and not workspace.ledger.reached(workspace.clock()):
            if last[2] == workspace.ledger.caps:  # the same caps, so a new UTC day lifted them: a raise says its own line
                _echo("Daily budget: a new UTC day began, so model calls resume")
            last.clear()
        now = [result.stops, sorted({str(c.get("at")) for c in result.crossed.values()}), workspace.ledger.caps]
        if result.stops and now == last:
            said.update(result.stops)
        else:
            said.difference_update(result.stops)
        _print(_masked(result, workspace.root), said=said)
        if result.stops:
            last[:] = now
    return printed


def _print_compiled(agent: str, article_id: str, compiled: Compiled, alone: bool = True, workspace: Any = None) -> str:
    """One agent's run by hand, and what grounding dropped and its notes, as a pass prints them. A run that ran and
    wrote nothing says why and is no failure, also when its article holds its
    earlier text. Gives why a run failed, or "": for a composed article, a subagent's failure fails the article's
    run. A run by hand runs even when nothing moved, and when its evidence holds nothing to cite, so it skips only
    when it waits: run `alone`, that ends the command; one of many articles gets a `skipped` line, and the rest
    run. An article out of the working set is left alone and is no failure,
    and one a person took out while its run was under way says what of it landed: nothing, with what it cost when
    `workspace` is given."""
    from .budget import spent_by_runs

    key = agent if article_id == agent else f"{agent} on {article_id}"
    runs = compiled.runs or ([compiled.run] if compiled.run is not None else [])
    if compiled.left_alone and runs:  # it ran, and a person took the article out of the working set meanwhile
        found = workspace.store.article(article_id) if workspace is not None else compiled.article
        why = (f"a person archived {article_id} while it ran" if found is not None and found.identity.status == "archived"
               else f"{article_id} left the working set while it ran")
        cost = f", ${spent_by_runs(workspace.store, [r.id for r in runs]):.4f}" if workspace is not None else ""
        _echo(f"ran      {article_id}: run {runs[-1].id}{cost}, and nothing of it landed: {why}, so it is left alone")
        return ""
    if compiled.left_alone:
        _echo(f"Nothing to run. {compiled.left_alone}.")  # no failure
        return ""
    if compiled.skipped:  # it waits, as after a failed run on the same evidence
        why = f"{article_id} was not run: {compiled.waiting or 'nothing it reads has moved'}"
        if alone:
            raise click.ClickException(f"{agent}: {why}")
        _echo(f"skipped  {key}: {why}")
        return ""
    run = _shown(compiled)  # a subagent's failure, never hidden by its composer's run that added nothing
    nothing = run is not None and run.status == WROTE_NOTHING and (compiled.article is None or not any(
        r.status == "done" or r.status == "budget spent" and r.result is not None for r in compiled.runs or [run]))
    if nothing and run is not None:
        _echo(f"ran      {key}: wrote nothing: {run.note or 'it handed in nothing'}")
    elif compiled.article is not None:
        article = compiled.article.identity.id
        _echo(f"wrote    {article}  articles/{article}.md")
    for line in [*compiled.dropped, *compiled.cleaned, *compiled.notes]:
        _echo(f"warning  {key}: {line}")
    if compiled.warning:
        _echo(f"warning  {compiled.warning}")
    if nothing or compiled.article is not None:
        return ""
    return failure(run, agent) if run else "no run"


def _money(value: float | None, digits: int = 2) -> str:
    """An amount to `digits` places, cents by default."""
    if value is None:
        return "no cap"
    return f"${value:,.{digits}f}"


def _digits(total: float, parts: list[float]) -> int:
    """The fewest places, two at least, at which the parts of a spend read as adding up to its total and no amount
    spent reads as nothing."""
    for digits in range(2, 7):
        if round(sum(round(p, digits) for p in parts), digits) == round(total, digits) and all(
                round(p, digits) > 0 for p in [total, *parts] if p > 0):
            return digits
    return 6


def _key_line(variable: str) -> str:
    provider = next((p.name for p in PROVIDERS.values() if p.variable == variable), variable)
    return (f"key       {variable} is not set: keep the key in the workspace's secrets store with cerebro secrets set "
            f"{provider}, or export {variable}=<your key> in the shell that runs cerebro")


# -- the root: what every command takes ----------------------------------

PLAIN = "cerebro.plain"  # in the context's meta, shared by every command under the root: --plain was given
WORKSPACE = "cerebro.workspace"  # and the workspace the last --workspace so far on the line named

def _plain(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if value:
        ctx.meta[PLAIN] = True


def _plain_option() -> click.Option:
    return click.Option(["--plain"], is_flag=True, expose_value=False, callback=_plain,
                        help="Print plain text: no colour, no drawing and no escape byte.")


def _workspace_option(help: str) -> click.Option:
    return click.Option(["--workspace"], type=click.Path(path_type=Path, file_okay=False), default=None,
                        expose_value=False, callback=_named_before, help=help)


class _Takes(click.Command):
    """What every command of Cerebro's takes, group or not: -h, --plain and --workspace at any place on its line,
    each with one meaning, a command's own where it has one.
    """

    WORKSPACE_HELP = "The workspace folder. The default is the current folder."

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        params = super().get_params(ctx)
        names = {param.name for param in params}
        added = [*([] if "plain" in names else [_plain_option()]),
                 *([] if names & {"root", "workspace"} else [_workspace_option(self.WORKSPACE_HELP)])]
        return [*params[:-1], *added, params[-1]]  # before the help option, which is last

    def invoke(self, ctx: click.Context) -> Any:
        if ctx.params.get("plain"):
            ctx.meta[PLAIN] = True  # a command's own --plain holds for the commands after it on the line
        if ctx.meta.get(PLAIN) and "plain" in ctx.params:
            ctx.params["plain"] = True  # --plain given before the command's name is the command's own --plain
        return super().invoke(ctx)


class _Leaf(_Takes):
    """A command of Cerebro's that is no group."""


class _Group(_Takes, click.Group):
    """A group of Cerebro's commands, each a `_Leaf`."""

    command_class = _Leaf
    group_class = type
    WORKSPACE_HELP = "The workspace folder, for the command after it. The default is the current folder."


class _Root(_Group):
    """The root command: -h and help, and its help on standard output, exit 0, with no command."""

    group_class = _Group

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("context_settings", {})["help_option_names"] = ["-h", "--help"]
        kwargs["invoke_without_command"] = True
        super().__init__(*args, **kwargs)


@click.group(cls=_Root)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Cerebro: a wiki your agents write and keep current, built from your sources."""
    if ctx.invoked_subcommand is None:  # no command: the help, on standard output
        click.echo(ctx.get_help())


@main.command("help")
@click.argument("words", nargs=-1)
@click.pass_context
def help_command(ctx: click.Context, words: tuple[str, ...]) -> None:
    """Print the help of the command WORDS name, as its --help does, or of cerebro itself."""
    at = ctx.find_root()
    for word in words:
        group = at.command
        command = group.get_command(at, word) if isinstance(group, click.Group) else None
        if command is None:
            at.fail(f"No such command '{word}'.")
        at = command.make_context(word, [], parent=at, resilient_parsing=True)
    click.echo(at.get_help())


_LOADS = {
    "bash": """Print the bash completion script. It needs bash 4.4 or later.

    For this shell only:

    \b
        source <(cerebro completion bash)

    For every new shell, save it once where the bash-completion package looks. On Linux:

    \b
        cerebro completion bash > /etc/bash_completion.d/cerebro

    On macOS with Homebrew:

    \b
        cerebro completion bash > $(brew --prefix)/etc/bash_completion.d/cerebro

    Open a new shell to use it.""",
    "zsh": """Print the zsh completion script.

    Completion must be on in zsh. If it is not, add this line to ~/.zshrc:

    \b
        autoload -U compinit; compinit

    For this shell only:

    \b
        source <(cerebro completion zsh)

    For every new shell, save it once in a folder on your fpath. On Linux:

    \b
        cerebro completion zsh > "${fpath[1]}/_cerebro"

    On macOS with Homebrew:

    \b
        cerebro completion zsh > $(brew --prefix)/share/zsh/site-functions/_cerebro

    Open a new shell to use it.""",
    "fish": """Print the fish completion script.

    For this shell only:

    \b
        cerebro completion fish | source

    For every new shell, save it once:

    \b
        cerebro completion fish > ~/.config/fish/completions/cerebro.fish

    Open a new shell to use it.""",
}


def _bare(complete: type[ShellComplete]) -> type[ShellComplete]:
    """A shell's completion with no description beside a command or an option.
    Its script asks under a name of its own, so the answers it gets carry none."""
    shell = complete.name

    class Bare(complete):  # type: ignore[valid-type, misc]
        name = f"{shell}bare"

        def source(self) -> str:
            return super().source().replace(f"={shell}_complete", f"={self.name}_complete")

        def format_completion(self, item: CompletionItem) -> str:
            return str(super().format_completion(CompletionItem(item.value, type=item.type)))

    add_completion_class(Bare)
    return Bare


class _ZshFromFpath(ZshComplete):
    """Zsh's script, whose function is named as the file the help writes it to, `_cerebro`. Autoloaded from fpath, a
    function named otherwise leaves `_cerebro` holding the whole file, and every new session's second Tab offers
    nothing."""

    @property
    def func_name(self) -> str:
        return "_cerebro"


_COMPLETES: dict[str, tuple[type[ShellComplete], type[ShellComplete]]] = {
    "bash": (BashComplete, BashComplete),  # bash's script shows no description either way
    "zsh": (_ZshFromFpath, _bare(_ZshFromFpath)), "fish": (FishComplete, _bare(FishComplete))}


@main.group("completion", invoke_without_command=True)
@click.pass_context
def completion_command(ctx: click.Context) -> None:
    """Print a script that completes cerebro's commands on Tab, for bash, zsh or fish. Each shell's help says how
    to load it."""
    if ctx.invoked_subcommand is None:  # no shell: the help, on standard output, exit 0
        click.echo(ctx.get_help())


def _completion(shell: str) -> None:
    @completion_command.command(shell, help=_LOADS[shell], short_help=f"Print the {shell} completion script.")
    @click.option("--no-descriptions", "bare", is_flag=True, help="Leave out the note beside each choice.")
    def script(bare: bool) -> None:
        complete = _COMPLETES[shell][bare]
        click.echo(complete(main, {}, "cerebro", "_CEREBRO_COMPLETE").source())


for _shell in _COMPLETES:
    _completion(_shell)


# -- init -----------------------------------------------------------------------------------------------------------

def _asking() -> bool:
    return sys.stdin.isatty()


def _zone_prompt(local: str | None) -> str:
    return "Time zone" if local else "Time zone (this machine names none, so UTC is the fallback)"


def _not_a_zone(name: str) -> str:
    return (f"{shown(name, 80) or 'an empty name'} is not a time zone Cerebro knows: give an IANA name such as America/Los_Angeles, "
            "Europe/Berlin or UTC")


def _budget_and_zone(daily_usd: float | None, no_cap: bool, timezone: str | None,
                     local: str | None, screen: Any) -> tuple[float | None, str | None]:
    """The daily budget and the time zone, asked on `screen`'s lines at a terminal where no option gave them. Once the
    input has ended, as with Ctrl-D, nothing more is asked and each takes its default;
    Ctrl-C ends init with 130, as at every line of the walk."""
    from .budget import BudgetError, read_caps

    while daily_usd is None and not no_cap:
        said = None if getattr(screen, "ended", False) else screen.text("Daily budget in dollars", default=f"{float(DAILY_USD)}")
        if said is None:
            daily_usd = DAILY_USD
            break
        ended = getattr(screen, "ended", False)  # an answer typed as the input ended cannot be asked again
        try:
            asked = float(said)
        except ValueError:
            if ended:
                raise CerebroError(f"{said!r} is not a valid float.") from None
            _echo(f"Error: {said!r} is not a valid float.")  # as the prompt's own check said it
            continue
        try:
            read_caps({"budget": {"daily_usd": asked}})  # checked as it is asked, so it is asked again
            daily_usd = asked
        except BudgetError as error:
            if ended:
                raise CerebroError(" ".join(str(error).split())) from None
            _echo(f"          {' '.join(str(error).split())}")
    while timezone is None:
        said = None if getattr(screen, "ended", False) else screen.text(_zone_prompt(local), default=local or "UTC")
        if said is None:
            timezone = local or "UTC"
        elif clock.is_zone(said):
            timezone = said
        elif getattr(screen, "ended", False):
            raise CerebroError(_not_a_zone(said).removeprefix("Error: "))
        else:
            _echo(_not_a_zone(said))
    return daily_usd, timezone


def _flagged(ctx: click.Context) -> bool:
    """Whether any option was given on the command line, before the command's name or after it, so init walks no
    one through."""
    from click.core import ParameterSource

    return ctx.meta.get(PLAIN, False) or ctx.meta.get(WORKSPACE) is not None or any(
        ctx.get_parameter_source(p.name) is ParameterSource.COMMANDLINE
        for p in ctx.command.params if p.name and p.name != "folder")


def _answered(ctx: click.Context) -> bool:
    """Whether init was given an answer on the command line: any option but the folder's and --plain, which answer
    no question."""
    from click.core import ParameterSource

    return any(ctx.get_parameter_source(p.name) is ParameterSource.COMMANDLINE
               for p in ctx.command.params if p.name and p.name not in ("folder", "root", "plain"))


DAILY_USD = scaffold.DAILY_USD  # the daily cap init writes when no answer gives one


def _run_tool(argv: Sequence[str]) -> tuple[int, str]:
    """Run a client's own command, as `claude mcp add`, and give its exit code and what it said."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _guided_init(folder: Path | None, printer: Any, asking: bool = True) -> None:
    """init at a terminal that draws, with no option: a new workspace from a blank start or a template, then the
    walk; a workspace already there, the walk alone, which shows what is set and offers the rest. Not `asking`, on a
    workspace there off a terminal or under --plain, the walk shows what is set and asks nothing."""
    from . import guided

    screen = guided.TerminalScreen(printer, _echo)
    screen.ended = not asking
    target = (folder or Path.cwd()).resolve()
    new = not (target / "settings.yaml").is_file()
    drawn = getattr(screen, "drawn", False)  # a test hands a scripted screen of its own, which draws nothing
    if drawn:  # the header: the command, then its steps under it
        printer.header("cerebro init", "" if new else shown(str(target), None))
    if new:
        names = scaffold.templates()
        got = screen.menu(guided.template_menu(names))
        if got is None:
            _echo("Nothing written.")
            return
        template = None if got == 0 else names[got - 1]
        target = scaffold.check_init(target, template=template)
        you = guided.ask_you(screen)
        local = clock.local_zone()
        daily_usd, timezone = _budget_and_zone(None, False, None, local, screen)
        done = scaffold.init(target, template=template, daily_usd=daily_usd, timezone=timezone or local,
                             environ=os.environ, command=_own_command(), name=you.name,
                             email=you.email, org=you.org, domains=you.domains)
        if drawn and not done.held:  # the summary takes the questions' place, each gone once answered
            screen.erase()
            _created(printer, done)
        else:
            _init_summary(done, timezone or local)
        if line := _public_mail(you.email, you.org, you.domains):
            _echo(f"check     {line}")
        if done.held:
            return
    else:
        _checked_workspace(target)  # the walk writes nothing into a workspace its owner has not trusted
        if not drawn:
            _echo(f"workspace {target}", cap=None)
        else:  # the summary of a workspace that is there
            from .output import Mark

            printer.line(printer.mark(Mark.DONE) + " Workspace at " + printer.link(str(target), str(target)))
            printer.line("  " + printer.dim("Edit ") + printer.link("workspace.md", str(target / "workspace.md"))
                         + printer.dim(" or ") + printer.link("profile.md", str(target / "profile.md"))
                         + printer.dim(" to set custom instructions."))
    ctx = click.get_current_context()
    checks, then = guided.Walk(target, screen, open_workspace, lobby=lambda: _lobby(target, printer, end=False),
                               first_run=lambda: ctx.invoke(serve, root=target, once=True, port=None, host=None,
                                                            allowed=(), under_init=True),
                               home=_client_home(), command=_own_command(), environ=_client_environ(),
                               run_tool=_claude(), prove=_prove_on(target), mark=printer.mark, new=new).all()
    if new and (line := _public_mail(you.email, you.org, you.domains)):
        checks.insert(0, line)
    for line in checks:  # what still waits, as the command's last line says it
        printer.check(line)
    if then:
        printer.next(then)
    if drawn:  # set apart from the summaries above it
        click.echo("", err=True)
    click.echo(printer.last_line(), err=True)


def _public_mail(email: str, org: str, domains: Sequence[str]) -> str:
    """The line for an address at a public mail service, when workspace.md is left without what it would have given:
    the team or organization's domain, its name, or both. "" otherwise."""
    from .connector import is_public_mail

    if not email or not is_public_mail(email):
        return ""
    domain = email.rpartition("@")[2].lower()
    if not org and not domains:
        return f"{domain} is public mail, so workspace.md has no team or organization domain or name: fill them in"
    if not domains:
        return f"{domain} is public mail, so workspace.md has no team or organization domain: fill it in"
    if not org:
        return f"{domain} is public mail, so workspace.md has no team or organization name: fill it in"
    return ""


def _client_home() -> Path:
    """The home folder the MCP clients keep their config under: the person's own, read only when the walk runs."""
    return Path.home()


def _client_environ() -> Mapping[str, str]:
    """The environment the walk reads the clients' own variables from, as `CLAUDE_CONFIG_DIR`: this process's. A seam,
    so the scripts in checks/ and the tests pass their own and never read the real one."""
    return os.environ


def _own_command() -> str:
    """The command that starts this Cerebro, for a client's entry: the
    `cerebro` script this process runs from, else the one on the PATH, else the bare name."""
    running = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if running is not None and running.name == "cerebro" and running.is_file():
        return str(running.resolve())
    return shutil.which("cerebro") or "cerebro"


def _prove_on(root: Path) -> Callable[[Any, str], None]:
    """A key's proof call on the workspace at `root`: one small call on the row's first job, the app's own, checked
    against its budget before it starts. A seam, so every test stands in for it and none reaches a provider."""

    def prove(row: Any, value: str) -> None:
        job, spec = row.jobs[0]
        with open_workspace(root) as workspace:
            workspace.prove_key(job, spec, value)

    return prove


def _claude() -> Callable[[Sequence[str]], tuple[int, str]] | None:
    """What runs Claude Code's own `claude mcp add`, or None when claude is not on the PATH."""
    return _run_tool if shutil.which("claude") else None


def _created(printer: Any, done: Any) -> None:
    """What init made, where the terminal draws: the check and the folder, then
    the two files to edit, muted, each a link to its file; then, muted, anything to notice about the folder."""
    from .output import Mark, fold

    root = Path(done.root)
    printer.line(printer.mark(Mark.DONE) + " Created new workspace in " + printer.link(str(root), str(root)))
    printer.line("  " + printer.dim("Edit ") + printer.link("workspace.md", str(root / "workspace.md"))
                 + printer.dim(" or ") + printer.link("profile.md", str(root / "profile.md"))
                 + printer.dim(" to set custom instructions."))
    notes = []
    if done.open_to_others:
        notes.append(f"The folder is open to other accounts on this machine, and so the articles under it: chmod 700 "
                     f"{shell_path(root)} closes it.")
    if done.repository is not None:
        notes.append(f"Made inside the git repository at {done.repository}: a workspace usually lives in a folder of "
                     "its own, so move it out unless you mean to keep it in that repository.")
    for note in notes:  # each wrapped at its spaces, never mid-word
        for row in fold("  " + printer.dim(note), printer.width(), "  "):
            printer.line(row)


def _labelled(line: str, cap: int | None = LINE_CAP) -> None:
    """A `label     words` line of init's, wrapped at its spaces to the terminal's width under its words at a
    terminal, so a word is never cut in two; off a terminal one line."""
    from .output import fold

    if not _terminal_out():
        _echo(line, cap=cap)
        return
    import shutil

    from .output import command_in

    said = shown(line, cap)
    label, words = said[:10], said[10:]
    if not label.endswith(" ") or not words:  # no label of its own: the line as it is
        click.echo(said)
        return
    command = command_in(words)  # a command to run is never broken across rows, as code is not
    if command:
        words = words.replace(command, command.replace(" ", "\x00"), 1)
    rows = [row.replace("\x00", " ") for row in fold(words, shutil.get_terminal_size().columns - 1 - len(label))]
    click.echo(label + rows[0])  # the label keeps its first word with it, a path among them
    for row in rows[1:]:
        click.echo(" " * len(label) + row)


def _init_summary(done: Any, timezone: str | None) -> None:
    """What init made: the folder, whether it is trusted, what it wrote, its zone and anything to notice. At a
    terminal each line wraps at its spaces under its words, never mid-word; off one each is one line."""
    _echo = _labelled  # the wrapping echo, under the name the lines below use
    _echo(f"workspace {done.root}", cap=None)
    if done.held:
        for line in done.held:
            _echo(line)
        _echo(f"trust     not trusted: this folder held what is listed above before init, and init trusts only what it "
              f"makes: read it, then run cerebro trust {shell_path(done.root)}", cap=None)
    top = ["settings.yaml", "workspace.md", *(["profile.md"] if "profile.md" in done.written else []), "AGENTS.md"]
    under: dict[str, int] = {}
    for rel in done.written:
        if rel not in top:
            folder_name = rel.split("/")[0]
            under[folder_name] = under.get(folder_name, 0) + 1
    _echo("wrote     " + ", ".join([*top, *(f"{_count(n, 'file')} under {f}/" for f, n in under.items())]))
    if done.ignored:
        _echo(f"ignored   {', '.join(done.ignored)} added to .gitignore")
    _echo(f"zone      {timezone}, written to settings.yaml as timezone:" if timezone else
          "zone      UTC, since this machine names no time zone: write timezone: in settings.yaml to change it")
    if done.open_to_others:
        _echo(f"notice    other accounts on this machine can read {shell_path(done.root)}, and so the articles under "
              f"it: chmod 700 {shell_path(done.root)} closes it", cap=None)
    if done.repository is not None:
        _echo(f"notice    made inside the git repository at {done.repository}: a workspace usually lives in a folder of "
              "its own, so move it out unless you mean to keep it in that repository", cap=None)


@main.command()
@click.argument("folder", required=False, type=click.Path(path_type=Path, file_okay=False))
@_root_option
@click.option("--template", default=None, metavar="NAME",
              help=f"Start from a bundled template's agents: {', '.join(scaffold.templates())}. The default is a blank "
                   "workspace, with no agent.")
@click.option("--daily-usd", type=float, default=None, help="The daily budget in dollars. The default is 50.")
@click.option("--no-cap", is_flag=True, help="Write no daily budget.")
@click.option("--timezone", default=None, help="An IANA time zone, such as America/Los_Angeles. The default is this "
              "machine's own, or UTC when it names none.")
@click.option("--domain", "domains", multiple=True, help="Your own email domain, such as example.com. Repeat for more.")
@click.option("--name", "your_name", default="", help="Your name, for profile.md.")
@click.option("--email", default="", help="Your work email, for profile.md. Its domain is the workspace's own, unless "
              "it is at a public mail service.")
@click.option("--org", default="", help="Your team or organization's name, for workspace.md. The default is a "
              "placeholder from your email's domain.")
@click.option("--plain", is_flag=True, help="Ask on plain lines, one question at a time: no walk is drawn.")
@_refusing
def init(folder: Path | None, root: Path | None, template: str | None, daily_usd: float | None, no_cap: bool,
         timezone: str | None, domains: tuple[str, ...], your_name: str = "", email: str = "", org: str = "",
         plain: bool = False) -> None:
    """Create a workspace in FOLDER, or the current folder: settings, a local/ folder and the files source over it, an
    AGENTS.md for any agent working there, and a .gitignore. It is blank,
    with no agent, unless --template names one. It stores no key, and writes no client's config.

    At a terminal that draws, with no option given, it walks you through the rest, and run again on a workspace it
    shows what is set and offers what is not: who you are, the workspace's own domains, the models and their keys,
    the sources, the MCP clients, each written only where you say so, and the first run."""
    # an option, --plain among them, means plain lines: no printer is built, so nothing is sent to the terminal
    if not _flagged(click.get_current_context()) and (printer := _drawing(False)) is not None:
        _guided_init(folder, printer)
        return
    if folder is not None and root is not None and folder.resolve() != root.resolve():
        raise _Whole(f"init was given two folders, {folder} and --workspace {root}: give one")
    given = folder or root or Path.cwd()
    if not _answered(click.get_current_context()) and (given / "settings.yaml").is_file():
        # run again on a workspace where nothing is asked: it shows what is set, and asks
        # nothing, so `cerebro init` there is always the way back to the steps a terminal offers
        from .output import Printer

        _guided_init(given, Printer(plain=True), asking=False)
        return
    if not _asking() and not _answered(click.get_current_context()) and not (given / "settings.yaml").is_file():
        # off a terminal it asks nothing, so a new workspace needs an answer for each
        # question, named in one line, exit 2, and nothing is written, not even the folder
        where = "" if folder is None and root is None else f" {shell_path(given)}"
        raise _Waits(f"init needs an answer for each question it would ask: run it in a terminal to be asked, or pass "
                     f"each of --daily-usd <dollars> or --no-cap, and --timezone <zone>, as cerebro init{where} "
                     f"--daily-usd {DAILY_USD:g} --timezone {clock.local_zone() or 'UTC'}")
    target = scaffold.check_init(given, template=template)
    email = scaffold.check_email(email)  # every answer a flag gives is checked before any question
    your_name, org = scaffold.check_text("name", your_name), scaffold.check_text("team or organization", org)
    domains = tuple(scaffold.own_domains(domains))
    local = clock.local_zone()
    if timezone is not None and not clock.is_zone(timezone):
        raise click.ClickException(f"--timezone: {_not_a_zone(timezone)}")
    if daily_usd is not None and not no_cap:
        scaffold.check_answers(daily_usd, None)
    if _asking():
        from . import guided

        # one question at a time, on a plain line, for what no flag gave
        screen = guided.TerminalScreen(None, _echo)
        you = guided.ask_you(screen, guided.You(your_name, email, org, domains))
        your_name, email, org, domains = you.name, you.email, you.org, you.domains
        daily_usd, timezone = _budget_and_zone(daily_usd, no_cap, timezone, local, screen)
    timezone = timezone or local
    budget = None if no_cap else (DAILY_USD if daily_usd is None else daily_usd)
    done = scaffold.init(target, template=template, daily_usd=budget,
                         timezone=timezone, domains=domains, environ=os.environ,
                         command=_own_command(), name=your_name, email=email, org=org)
    _init_summary(done, timezone)
    if line := _public_mail(email, org, domains):
        _labelled(f"check     {line}")
    for name in done.missing_keys:
        _labelled(_key_line(name))
    entry = done.mcp
    command = " ".join(shell_path(word) for word in [entry["command"], *entry["args"]])
    _echo("mcp       add the workspace to an MCP client. For Claude Code:")
    _echo(f"            claude mcp add --scope user {entry['name']} -- {command}", cap=None)
    _echo("          or, in a client's JSON config, such as Claude Desktop's or Cursor's:")
    snippet = {"mcpServers": {entry["name"]: {"command": entry["command"], "args": entry["args"]}}}
    for line in json.dumps(snippet, indent=2).splitlines():
        _echo(f"            {line}", cap=None)
    where = "" if done.root == Path.cwd().resolve() else f" --workspace {shell_path(done.root)}"
    if template is None:
        _labelled(f"next      write a first context agent: cerebro agents new <collection>/<name>{where}", cap=None)
        _labelled("next      or start from the gtm template: cerebro init --template gtm <a new folder>")
    else:
        _labelled(f"next      put notes as .md files in local/, then cerebro serve --once{where}", cap=None)
    _labelled(f"next      to read a source that needs a login, write sources/<name>.yaml, then cerebro connect <name>"
               f"{where}", cap=None)


@main.command("trust")
@click.argument("folder", required=False, type=click.Path(path_type=Path, file_okay=False))
@_root_option
@click.option("--new-id", "new_id", is_flag=True, help="First give the workspace a fresh id in settings.yaml, as a copy "
              "of a workspace on this machine needs: two workspaces do not share one secrets store.")
@_refusing
def trust_command(folder: Path | None, root: Path | None, new_id: bool) -> None:
    """Trust the workspace in FOLDER, or the current folder: cerebro runs a workspace only from a folder you trust. It
    prints what that allows, its code, scripts and source commands, each source and where it points, and its agents,
    then keeps the folder's real path and the workspace's id in your list of trusted workspaces, outside the workspace.
    A copy of a workspace on this machine shares its id: --new-id gives it one of its own."""
    from .secrets import claimed_by

    if folder is not None and root is not None and folder.resolve() != root.resolve():
        raise _Whole(f"trust was given two folders, {folder} and --workspace {root}: give one")
    target = (folder or root or Path.cwd()).resolve()
    if not (target / "settings.yaml").is_file():
        raise _Whole(f"{target} is not a workspace (no settings.yaml in it): name the workspace's folder")
    try:
        known = trusting.trusted(target)
    except CerebroError:
        known = True  # a record names the store, which does not open: the file's check names the line, if it can
    if known:
        _checked_workspace(target, trust=False)  # before trusting opens the store the file names, as every command does
    if new_id:
        old, fresh = scaffold.new_id(target)
        was = f", in place of {shown(old, trusting.CAP)}" if old else ""
        _echo(f"id        {fresh} in {target / 'settings.yaml'}{was}", cap=None)
        _echo("note      the new id opens an empty secrets store: keep the keys again with cerebro secrets set "
              "<name>, or export them")
    workspace_id = trusting.settings_id(target)
    if workspace_id and (other := claimed_by(workspace_id, target)):
        raise _Whole(
            f"workspace id {shown(workspace_id, trusting.CAP)}, the id: key in {target / 'settings.yaml'}, belongs to the "
            f"workspace at {other}, so this copy cannot open its own secrets store: run cerebro trust --new-id {shell_path(target)}")
    for line in trusting.allows(target):
        _echo(line, cap=None)
    _echo(f"trusted   {trusting.trust(target)}", cap=None)
    _checked_workspace(target)


# -- status, agents, compile ----------------------------------------------------------------------------------------

def _cursor_line(cursor: dict[str, Any]) -> str:
    """What `status` says of one source's cursor and its last listing in full."""
    if not cursor["cursor"]:
        return "none, so the next check lists the source in full"
    if not cursor["changes"]:
        return "held, and each check reads the source as its connector does"
    if cursor.get("always"):
        return "held, and each check lists the source in full, by choice, as full_every: always asks"
    full = f"last listed in full at {cursor['full']}" if cursor["full"] else "not listed in full yet"
    return f"held, so a check lists what changed since it; {full}"


@main.command()
@_root_option
@_json_option
@_refusing
def status(root: Path | None, as_json: bool) -> None:
    """What is in the workspace, what today cost, and what it is missing. For a workspace you have not trusted, what
    trusting it would allow, with nothing of it imported or run."""
    target = (root or Path.cwd()).resolve()
    if (target / "settings.yaml").is_file() and not trusting.trusted(target):
        allowed = trusting.allows(target)
        if as_json:
            _print_json({"root": str(target), "trusted": False, "allows": allowed})
            return
        _echo(f"workspace {target}", cap=None)
        _echo(f"trust     not trusted, so cerebro runs nothing of it: cerebro trust {shell_path(target)} would allow this",
              cap=None)
        for line in allowed:
            _echo(line, cap=None)
        return
    from . import standing
    from .gateway import calls_go_to, route_lines
    from .project import zone_of

    with open_workspace(root) as workspace:
        data = workspace.status()
        data["cursors"] = workspace.cursors()
        data |= standing.gather(workspace, data)
        data["waits"] = waiting_runs(workspace) + reviewing.held_lines(workspace)
        data |= {"calls_go_to": hosts} if (hosts := calls_go_to(workspace.settings)) else {}
        zone, held_to, routes = zone_of(workspace.settings), workspace.ledger.caps, route_lines(workspace.settings)
    data["refused"] = list(map(_masking(data["refused"], root), data["refused"]))
    if as_json:
        _print_json(data)
        return
    for reached in data["caps_reached"]:
        _echo(standing.cap_line(reached, zone))
    _echo(f"workspace {data['root']}", cap=None)
    if data["id"]:
        _echo(f"id        {data['id']}")
    _echo(f"timezone  {data['timezone']}")
    for line in routes:
        _echo(line)
    _echo(f"agents    {data['agents']} loaded")
    _echo(f"sources   {', '.join(data['sources']) or 'none'}")
    for name, check in data["checks"].items():
        if not check:
            _echo(f"checked   {name}: never, so the next pass checks it")
            continue
        stopped = "" if check.get("finished") else ", and it did not finish"
        _echo(f"checked   {name}: last at {check['at']}{stopped}, next due at {check['next']} under serve")
    for name, cursor in data["cursors"].items():
        _echo(f"cursor    {name}: {_cursor_line(cursor)}")
    if not data["facts_layer"]:
        _echo("facts     no facts layer is plugged in, so no source's records are read yet")
    else:
        facts = data["facts"]
        where = ", ".join(f"{n} from {source}" for source, n in facts["by_source"].items())
        _echo(f"facts     {facts['current']} current, from {_count(facts['records'], 'record')}"
                   + (f": {where}" if where else ": a pass reads the sources, as cerebro serve --once"))
        if dropped := facts.get("dropped"):
            why = ", ".join(f"{name} {n}" for name, n in dropped["why"].items())
            _echo(f"dropped   {_count(dropped['candidates'], 'candidate fact')} from "
                  f"{_count(dropped['records'], 'record')} did not pass the facts layer's checks, so "
                  f"{'it was' if dropped['candidates'] == 1 else 'they were'} not kept" + (f": {why}" if why else ""))
    if waiting := data["tells_waiting_ids"]:  # every id is named, not only a count
        _echo(f"underived {_count(len(waiting), 'tell')} whose facts have not derived yet: {', '.join(waiting)}. The "
              "next pass tries again.", cap=None)
    shown = ", ".join(f"{c}: {n}" for c, n in data["articles"].items())
    _echo(f"articles  {shown or 'none yet'}")
    if waits := data["pending"]:
        _echo(f"pending   {_count(len(waits), 'article')} not yet written:")
        for id in waits[:PENDING_SHOWN]:
            _echo(f"{_GOES_ON}{id}")
        if len(waits) > PENDING_SHOWN:
            _echo(f"{_GOES_ON}and {len(waits) - PENDING_SHOWN} more")
    if data["requests"]["total"]:
        _echo(f"requests  {_rates(data['requests'])}")
    _echo(f"working   {_count(data['working_set'], 'article')} in the working set")
    if data["archived"]:
        _echo(f"archived  {_count(data['archived'], 'article')} out of the working set")
    if data["compile_order"]:
        _echo(f"order     {data['compile_order']}")
    for agent, run in data["last_runs"].items():
        why = f": {run['note']}" if run.get("note") else ""
        _echo(f"ran       {agent}: {run['status']} at {run['ended_at']} on {run['model']}{why}")
    _failing_lines(data)
    for line in data["waits"]:  # a run that waits after it failed or stopped at its budget, and until when
        _echo(f"waiting   {line}")
    if data["waiting"]:
        waiting = data["waiting"]
        _echo(f"review    {waiting} proposal{'' if waiting == 1 else 's'} {'is' if waiting == 1 else 'are'} "
                   f"awaiting review: decide {'it' if waiting == 1 else 'them'} with cerebro review")
    for said in data["instructions"]:  # every instruction that stands for an article's writers, a person's first
        who = (f"{said['by']} instructs its writers, until a person gives theirs" if said["kind"] == "agent" else
               f"{said['by']}, {KIND_WORDS.get(said['kind'], said['kind'])}, instructs its writers")
        _echo(f"instruct  {said['article']}: {who}: {one_line(said['text'], 200)}")
    for expire in data["expires"]:
        _echo(f"expire    {expire['id']} flags \"{expire['title']}\" ({expire['target']}) as out of date, awaiting "
              f"review: {expire['reason'] or 'no reason given'}")
    spent, caps = data["spent_today"], data["caps"]
    rerank = data["rerank"]
    if rerank["on"]:
        click.echo(f"search    matching re-ranks its best {rerank['top']} hits on {rerank['model']}")
    elif rerank["asked"]:
        click.echo("search    search.rerank is on, and with no decision model matching keeps its search order")
    shown = ["extraction", "compilation"] + (["evaluation"] if spent.get("evaluation") else []) + (
        ["rerank"] if rerank["on"] or spent.get("rerank") else [])  # evals when run, re-ranking when on
    digits = _digits(spent["total"], [spent[layer] for layer in shown])
    layers = [f"{layer} {_money(spent[layer], digits)}" + (f" of {_money(caps[layer])}" if caps[layer] is not None
                                                            else "") for layer in shown]
    _echo(f"spent     today {_money(spent['total'], digits)} of {_money(caps['total'])}: {', '.join(layers)}")
    for name in data["missing_keys"]:
        _echo(_key_line(name))
    for own in data["agent_keys"]:
        _echo(f"key       agents/{own['agent']}.md runs on {own['provider']} {own['model']}, and no key for it is kept "
              f"or set: run cerebro secrets set {own['provider']}, or export {own['variable']}")
    for model in data["unpriced"]:
        _echo(f"price     {standing.price_line(model, held_to)}")
    for line in data["kinds"]:
        _echo(f"notice    {line}")
    for name in data["missing_logins"]:
        _echo(f"login     sources/{name}.yaml needs a login, and none is kept: run cerebro connect {name}")
    for line in data["refused"]:
        _echo(f"refused   {line}")
    for line in data["notices"]:
        _echo(f"notice    {line}")
    if data["grounding"]:  # a total, then a line an article, counts alone
        from .ground import counted

        total: Counter[str] = Counter()
        for tally in data["grounding"].values():
            total.update(tally)
        n = len(data["grounding"])
        _echo(f"grounding {n} article{'' if n == 1 else 's'}: {counted(total)}")
        for target, tally in sorted(data["grounding"].items()):
            _echo(f"{_GOES_ON}{target}: {counted(tally)}")
    if data["check"]:
        _closing(data["check"])



_GOES_ON = " " * 10  # a line that goes on with the line above it, under its words
_NAMED = 5  # records named on a source's line of the failed lines; the rest are counted
PENDING_SHOWN = 5  # pending articles `status` names, one a line; the rest are counted


def _closing(checks: list[str]) -> None:
    """The last line of `status` when something needs a look: done with warnings, and what to check, in the shape of
    `output.Printer`'s plain last line. It is written as every other line of `status` is, plain, so it carries no
    escape code and asks the terminal nothing."""
    more = f" (and {len(checks) - 1} more above)" if len(checks) > 1 else ""
    _echo(f"[!] Done with warnings. Check: {checks[0]}{more}", err=True)


def _rates(requests: dict[str, int]) -> str:
    """The share of requests that found what they asked for, and of those that missed, adding up to 100."""
    total = requests["total"]
    hit = (requests["hits"] * 100 + total // 2) // total
    return f"{hit}% hit ({requests['hits']} of {total}), {100 - hit}% miss ({requests['misses']} of {total})"


def _failing_lines(data: dict[str, Any]) -> None:
    """The failing, held and failed lines of `status`: each agent whose last run failed, each run held after failing
    and when it runs again, and the records each pass tries again, by source."""
    if failing := data["failing"]:
        _echo(f"failing   {_count(len(failing), 'agent')} whose last run failed, each tried again when next due or "
              "by cerebro agents run <id>")
        for agent in failing:
            since = f", failing since {agent['since']}" if agent["since"] != agent["at"] else ""
            why = f": {agent['reason']}" if agent["reason"] else ""
            _echo(f"{_GOES_ON}{agent['agent']} at {agent['at']}{since}{why}")
    if held := data["held_runs"]:
        _echo(f"held      {_count(len(held), 'agent run')} held after failing, each until the time shown, its evidence "
              "moves or its schedule comes")
        for run in held:
            _echo(f"{_GOES_ON}{run['agent']} on {run['article']}: failed {_count(run['times'], 'time')}, runs again "
                  f"after {run['until']}" + (f": {run['reason']}" if run["reason"] else ""))
    if records := data["failed_records"]:
        one = len(records) == 1
        _echo(f"failed    {_count(len(records), 'record')} gave no facts on {'its' if one else 'their'} last try, and "
              f"each pass tries {'it' if one else 'them'} again")
        by_source: dict[str, list[str]] = {}
        for record in records:
            by_source.setdefault(record["source"], []).append(record["id"])
        for source, ids in by_source.items():
            more = f", and {len(ids) - _NAMED} more" if len(ids) > _NAMED else ""
            _echo(f"{_GOES_ON}{source}: {', '.join(ids[:_NAMED])}{more}")


@main.group()
def agents() -> None:
    """The context agents of the workspace."""


@agents.command("list")
@_root_option
@_json_option
@_refusing
def agents_list(root: Path | None, as_json: bool) -> None:
    """The agents in this workspace, each with its role, target and heartbeat, and each file that did not load."""
    with open_workspace(root) as workspace:
        data = workspace.agent_list()
    data["refused"] = list(map(_masking(data["refused"], root), data["refused"]))
    if as_json:
        _print_json(data)
        return
    for row in data["agents"]:
        last = f"  last run {row['last_run']}" if row["last_run"] else ""
        beat = ", ".join(row["heartbeat"]) or ("its subagents'" if row["role"] == "composer" else "none")
        _echo(f"{row['id']}  {row['role']} of {row['target']}  heartbeat {beat}{last}")
    for line in data["refused"]:
        _echo(f"refused  {line}")
    _echo(f"{_count(len(data['agents']), 'agent')}, {len(data['refused'])} refused.")


@agents.command("new")
@click.argument("id")
@_root_option
@_refusing
def agents_new(id: str, root: Path | None) -> None:
    """Scaffold agents/ID.md: front matter and a prompt to fill in. The folder is the collection, and the name is
    the role: curator, article, subagents/<name>, or any other name for one article."""
    _checked_workspace(root)
    made = scaffold.new_agent(root or Path.cwd(), id)
    _echo(f"wrote    {made.path}")
    for line in made.notes:
        _echo(f"note     {line}")
    collection, _, name = id.partition("/")
    listed = "" if not made.notes or not name.startswith("subagents/") else \
        f", list {name.rpartition('/')[2]} under subagents: in agents/{collection}/article.md"
    one = " <article>" if name == "article" or name.startswith("subagents/") else ""  # it writes many: name one
    _echo(f"next     fill in {made.path}{listed}, then cerebro agents run {id}{one} --show-prompt")


@agents.command("run")
@click.argument("id")
@click.argument("article", required=False)
@_root_option
@click.option("--show-prompt", "show_prompt", is_flag=True,
              help="Print the prompt and evidence a run starts from, and call no model.")
@_refusing
def agents_run(id: str, article: str | None, root: Path | None, show_prompt: bool) -> None:
    """Run one agent now, as a pass's scheduled run does: a curator on its collection, a writer on each article it
    writes, or on the one ARTICLE names."""
    with open_workspace(root) as workspace:
        agent = workspace.agent(id)
        if agent is None and (why := refusal_of(workspace.refused, id)):
            raise click.ClickException(workspace.mask_every()(f"agents/{id}.md is refused, so it does not run: {why}"))
        if agent is None:
            raise click.ClickException(f"there is no agent {id} in agents/: run cerebro agents list")
        if show_prompt:
            for line in preview.shown_lines(preview.show(workspace, agent, article or "")):
                _echo(line, cap=None)
            return
        if agent.role == "curator" and article:
            raise click.ClickException(f"{id} runs on its collection, not on one article: give no article")
        if agent.role == "curator":
            from . import first_run

            with first_run.by_hand(workspace, agent, _echo) as count, workspace.hold(agent):  # its admits join the list
                curated = workspace.curate(agent)
                count(curated.run)
            for kind in ("admitted", "archived", "merged", "split", "proposed"):
                for article_id in getattr(curated, kind):
                    _echo(f"{kind:<8} {article_id}")
            for line in curated.notes:
                _echo(f"note     {line}")
            if first_run.in_first_run(workspace):
                if not curated.proposed:
                    _echo(f"filed     nothing for {agent.target} collection, by agents/{agent.id}.md")
                _echo("waiting   the workspace's first run is under way, so what a curator proposes is a row of its "
                      "list, and nothing is admitted until it is decided: cerebro serve --once shows it")
                _echo("note     a run by hand is not held to the first run's budget.first_run_usd, and what it "
                      "spent counts toward it")
            run = curated.run
            if run is not None and run.status in (WROTE_NOTHING, "budget spent"):  # no failure
                # ending with nothing new: what it decided has landed, and the line says what it did
                did = ", ".join(f"{kind} {len(getattr(curated, kind))}" for kind in ("admitted", "archived", "proposed")
                                if getattr(curated, kind))
                why = run.note if run.status == WROTE_NOTHING else run.status + (f": {run.note}" if run.note else "")
                _echo(f"ran      {id}: {did or 'changed nothing'}, {why}")
            elif run is not None and run.status != "done":
                raise click.ClickException(f"{id}: run {run.id} {run.status}" + (f": {run.note}" if run.note else ""))
            return
        lists = workspace.agent(agent.composers[0]) if agent.role == "subagent" and agent.composers else None
        only = agent.id if lists is not None else ""  # it runs alone, inside the run of the agent that lists it
        lists = lists or agent
        articles = sorted(targets(lists, workspace.lookup))
        if article:
            try:
                articles = [preview.pick(agent, articles, article, lambda named: preview.status_of(workspace.store, named))]
            except preview.Archived as archived:
                _echo(f"{archived.article} is archived, left alone")
                return
        if not articles and (why := left_alone(lists, workspace.lookup)):
            _echo(f"Nothing to run. {why}.")  # no failure
            return
        if not articles:
            raise click.ClickException(f"{agent.target} holds no article yet for {id} to write: its curator admits "
                                       "them, or run cerebro agents run on that curator")
        with workspace.hold(lists):
            for line in workspace.ready_by_hand(lists, articles):
                _echo(f"warning  {line}")
            from .app import MissingModelKey
            from .budget import BudgetSpent, HeldForRoom, UnpricedModel

            # Every article runs, past the cap the rest wait, and a run that left anything
            # unfinished is no period's run.
            waiting, stopped, began, left = [], None, workspace.clock(), PassResult()
            failed: dict[str, str] = {}  # why each article that failed did, by its id
            skipped = 0
            for article_id in articles:  # every one: one that skips or fails stops none
                key = lists.id if article_id == lists.id else f"{lists.id} on {article_id}"
                if stopped is not None:
                    waiting.append(article_id)
                    continue
                try:
                    compiled = workspace.compile(lists, this=article_id, scheduled=True, only=only)
                except HeldForRoom:  # every part held: one line, with when it truly runs
                    left.held[key] = f"held for room under the cap, {held_runs(workspace, lists, article_id)}"
                    continue
                except BudgetSpent as error:
                    if isinstance(error, UnpricedModel):
                        raise
                    stopped = error
                    waiting.append(article_id)
                    continue
                except MissingModelKey:  # the same for every article: the whole command is refused in one line
                    raise
                except Exception as error:  # noqa: BLE001 - one article's failure is its line, and the rest run
                    why = str(error) if isinstance(error, CerebroError) else f"{type(error).__name__}: {error}"
                    failed[article_id] = " ".join(why.split())
                    if len(articles) > 1:
                        _echo(f"failed   {key}: {failed[article_id]}")
                    continue
                skipped += compiled.skipped
                before, waited = set(left.failed), len(left.waiting_runs)
                left_by_hand(workspace, lists, article_id, compiled, left)
                why = _print_compiled(lists.id, article_id, compiled, alone=len(articles) == 1, workspace=workspace)
                new = {told: left.failed[told] for told in left.failed if told not in before}
                cut = [f"{run.agent} on {article_id}" for run in compiled.runs if run.id and run.status == "budget spent"
                       and not run.result]
                if why and cut and all(told in left.waiting_runs[waited:] for told in cut):
                    continue  # it waits for the daily cap, as a pass's run does: its waiting line says so
                if why or new and len(articles) == 1:  # one line for the article, with its runs' own reason
                    failed[article_id] = "; ".join(new.values()) or why
                    for told in new:
                        del left.failed[told]
                    if len(articles) > 1:
                        _echo(f"failed   {key}: {failed[article_id]}")
                elif new:
                    failed[article_id] = next(iter(new.values()))  # a part failed and the article stands: its line says it
            for kind, lines in (("held     ", left.held), ("failed   ", left.failed)):
                for told, why in lines.items():
                    _echo(f"{kind}{told}: {why}")
            if stopped is None and not only and not failed and not skipped and not (left.held or left.waiting_runs):
                ran_by_hand(workspace, lists, began)  # its whole job done, so a run by hand counts as its period's run
            if stopped is not None or left.waiting_runs:
                why, stops = waits_by_hand(workspace, stopped or BudgetSpent(left.stopped["compilation"]),
                                           [f"{lists.id} on {article_id}" for article_id in waiting] + left.waiting_runs)
                for told in [*waiting, *left.waiting_runs]:
                    _echo(f"waiting  {told}: {why}")
                for line in stops:
                    _echo(line)
            if len(articles) == 1 and failed.get(articles[0]):
                raise click.ClickException(f"{lists.id}: {failed[articles[0]]}")
            if failed:
                raise click.ClickException(f"{lists.id}: {len(failed)} of {len(articles) - skipped - len(waiting)} runs "
                                           "failed")
            if skipped == len(articles) > 1:
                raise click.ClickException(f"{lists.id}: none of its {len(articles)} articles holds anything to cite "
                                           "yet, so none was run")
            if (stopped is not None or left.waiting_runs) and not workspace.ledger.recorded:  # nothing was done: the
                raise SystemExit(1)  # cap is the fix


@main.command("review")
@_root_option
@click.option("--accept", "accept", multiple=True, metavar="ID", help="Accept the proposal ID, and ask nothing. "
              "Repeat, or separate ids with commas, for more.")
@click.option("--reject", "reject", multiple=True, metavar="ID", help="Reject the proposal ID, and ask nothing. "
              "Repeat, or separate ids with commas, for more.")
@click.option("--accept-from", "accept_from", multiple=True, metavar="FILE",
              help="Accept every admit awaiting your review that the curator FILE filed, such as "
                   "agents/things/curator.md, but those --reject names, and ask nothing. Repeat, or separate files "
                   "with commas, for more.")
@click.option("--reject-from", "reject_from", multiple=True, metavar="FILE",
              help="Reject every admit awaiting your review that the curator FILE filed, which defers each, but "
                   "those --accept names, and ask nothing. Repeat, or separate files with commas, for more.")
@_json_option
@_refusing
def review_command(root: Path | None, accept: tuple[str, ...], reject: tuple[str, ...], as_json: bool,
                   accept_from: tuple[str, ...] = (), reject_from: tuple[str, ...] = ()) -> None:
    """Show the proposals awaiting your review. At a terminal they open as the first run's list does: approve what
    you select, or press r on a row to reject it. With --plain it walks each, what it would change, who filed it,
    when and why, to accept, reject or skip. With --json it prints them and asks nothing. With --accept, --reject, --accept-from or
    --reject-from it decides those and asks nothing: a first run's list is decided this way off a terminal. Off a
    terminal it asks nothing."""
    # each value is read as one line of CSV: spaces and empty items kept
    accept, reject, accept_from, reject_from = (
        tuple(id for given in option for id in _named(given, name)) for option, name in
        ((accept, "--accept"), (reject, "--reject"), (accept_from, "--accept-from"), (reject_from, "--reject-from")))
    # an id in one list twice is refused before anything is decided; one in
    # both lists is refused by decide, which also decides nothing
    twice = next((id for given in (accept, reject) for n, id in enumerate(given) if id in given[:n]), None)
    if twice is not None:
        raise CerebroError(f"proposal {twice} is named twice, so nothing was decided: name each id once, in "
                           "--accept or --reject")
    with open_workspace(root) as workspace:
        from . import first_run

        admits = {item.proposal.id for item in workspace.review(YOU) if item.proposal.kind == "article.admit"}
        firsts: list[tuple[str, str, str]] = []
        named: set[str] = set()
        taken: dict[str, list[str]] = {"--accept-from": [], "--reject-from": []}
        for flag, files in (("--accept-from", accept_from), ("--reject-from", reject_from)):
            for file in files:  # what the first run's list showed, when its stamp is at hand, and no newer row
                agent = reviewing.agent_named(workspace, file, flag)
                if workspace.agent(agent).role != "curator":
                    raise CerebroError(f"review {flag} {file}: agents/{agent}.md curates no collection: name "
                                       "a curator cerebro agents list shows, such as agents/things/curator.md")
                if agent in named:  # one curator named twice is refused before any decide
                    raise CerebroError(f"agents/{agent}.md is named twice, so nothing was decided: name each curator "
                                       "once, in --accept-from or --reject-from")
                named.add(agent)
                until = first_run.shown_at(workspace, agent)
                # a curator's flag decides its admits alone: its other proposals wait
                shown = [id for id in reviewing.filed_by(workspace, YOU, file, until=until, flag=flag) if id in admits]
                if not shown:  # a curator with no admit waiting is refused, exit 1
                    later = _newer_than(workspace, file, flag, until)
                    raise CerebroError(f"no admit from {file} is awaiting your review" + (
                        f" that the first run's list showed, so nothing was decided: {_newer(later)}" if later else
                        ", so nothing was decided: cerebro review lists what is"))
                if until:
                    firsts.append((file, flag, until))
                taken[flag] += shown
        # an id --accept or --reject names stands as named: the ids are decided before the curators'
        accept = tuple(dict.fromkeys([*accept, *(id for id in taken["--accept-from"] if id not in reject)]))
        reject = tuple(dict.fromkeys([*reject, *(id for id in taken["--reject-from"] if id not in accept)]))
        if accept or reject:
            from .records import DecideResult

            spaced = [id for id in (*accept, *reject) if id != id.strip() or not id]  # decide would strip them
            result = workspace.masked(DecideResult({id: "unknown" for id in spaced},
                                                   {id: f"there is no proposal {id!r}" for id in spaced})) if spaced \
                else workspace.decide(YOU, list(accept) or None, list(reject) or None)
            stood = reviewing.standing(workspace, result, list(accept), list(reject))
            if refused := reviewing.refused(result, stood):  # nothing of the line was kept
                faults = type(result)({id: result.answers[id] for id in refused}, {id: result.notes[id] for id in refused})
                if as_json:
                    _print_json(faults)
                else:
                    for line in reviewing.result_lines(faults):
                        _echo(line)
                raise SystemExit(1)
            if as_json:
                _print_json(result)
            else:
                for line in reviewing.result_lines(result, stood):
                    _echo(line)
                newer = list(dict.fromkeys(id for file, flag, until in firsts
                                           for id in _newer_than(workspace, file, flag, until)))
                if newer:
                    _echo(f"waiting   {_newer(newer)}")
            if _kept(workspace, result):
                raise SystemExit(1)
            return
        if as_json:
            _print_json(reviewing.waiting(workspace, YOU))
            return

        def ask(text: str) -> str:
            return str(click.prompt(text, default="", show_default=False))

        printer = _drawing(bool(click.get_current_context().meta.get(PLAIN)))
        if printer is not None and _review_screen(workspace, printer):
            return
        walked = reviewing.walk(workspace, YOU, ask=ask if _asking() else None, say=_echo)
        held = reviewing.held_lines(workspace)
    _echo(reviewing.summary(walked))
    for line in held:
        _echo(f"waiting   {line}")


def _review_screen(workspace: Any, printer: Any) -> bool:
    """The review on the first run's screen, then its end lines, or False where it cannot draw or no proposal of an
    article waits, so the walk asks. A proposal of no article is left to the walk, and said so."""
    from .checklist import NoRawKeys
    from .first_run_list import draw
    from .review_list import ended, others, screen

    try:
        walked = screen(workspace, YOU, lambda pile, reject: draw(pile, printer, reject))
    except NoRawKeys:
        return False
    if walked is None:
        return False
    for line in ended(walked, others(workspace, YOU)):
        _echo(line)
    for line in reviewing.held_lines(workspace):
        _echo(f"waiting   {line}")
    return True


def _kept(workspace: Any, result: Any) -> list[str]:
    """The facts a decide's accepted retracts withdrew in the store that the facts layer kept all the same."""
    from .records import RETRACTS, proposals

    extraction = getattr(workspace.lookup, "extraction", None)
    withdrew = [str(f) for p in proposals(workspace.store) if p.kind in RETRACTS and p.status == "accepted"
                and result.answers.get(p.id) == "decided" for f in p.applied.get("withdrew") or []]
    return [f.id for f in extraction.facts(ids=withdrew) if f.status != "retracted"] if extraction and withdrew else []


def _newer_than(workspace: Any, file: str, flag: str, until: str) -> list[str]:
    """The proposals of any kind the curator of `file` filed after `until`, the stamp of the first run's list, that
    still await your review."""
    shown = set(reviewing.filed_by(workspace, YOU, file, until=until, flag=flag))
    return [id for id in reviewing.filed_by(workspace, YOU, file, flag=flag) if id not in shown]


def _named(value: str, option: str) -> list[str]:
    """The ids or files one value of `option` names, read as `_comma_list` reads it. A value that names nothing, empty
    or only commas and spaces, is a usage error."""
    items = _comma_list(value, option)
    if not any(item.strip() for item in items):
        what, give = ("curator", "a file") if option.endswith("-from") else ("proposal", "an id")
        raise click.UsageError(f"invalid argument {json.dumps(value, ensure_ascii=False)} for {json.dumps(option)} "
                               f"flag: it names no {what}: give {give}, or leave the flag out")
    return items


def _comma_list(value: str, option: str) -> list[str]:
    """`value` read as one record of comma separated fields, as a list flag takes it, or none when it is empty or holds
    only line breaks. Spaces and empty items are kept, so `a, b` gives ` b`. A field in double quotes may hold a comma
    or a line break, and `""` in one is a quote, so an id or a path with a comma in it can be given. A stray quote is a
    usage error that names its line and column. A value with more on a later line is refused too, so nothing given is
    dropped."""
    from .connectors.files import AFTER_QUOTE, NEVER_CLOSED, QUOTE_IN_VALUE

    data, at, number = value.encode(), 0, 0

    def read_line() -> tuple[bytes, bool]:  # the next line with its \n, \r\n as \n; True once the value is read
        nonlocal at, number
        number += 1
        if at >= len(data):
            return b"", True
        end = data.find(b"\n", at)
        got = data[at:] if end < 0 else data[at:end + 1]
        at += len(got)
        if end < 0 and got.endswith(b"\r"):
            got = got[:-1]
        return (got[:-2] + b"\n" if got.endswith(b"\r\n") else got), False

    def refused(why: str) -> click.UsageError:
        return click.UsageError(f"invalid argument {json.dumps(value, ensure_ascii=False)} for {json.dumps(option)} "
                                f"flag: {why}")

    def parse_error(row: int, column: int, why: str) -> click.UsageError:
        return refused(f"line {row}, column {column}: {why}")

    text, ended = read_line()
    while not ended and text in (b"", b"\n"):  # empty lines are skipped, a lone \r at the end among them
        text, ended = read_line()
    if ended:
        return []
    fields: list[bytes] = []
    column = 1
    while True:
        if not text.startswith(b'"'):
            comma = text.find(b",")
            field = text[:comma] if comma >= 0 else text.removesuffix(b"\n")
            if (quote := field.find(b'"')) >= 0:
                raise parse_error(number, column + quote, QUOTE_IN_VALUE)
            fields.append(field)
            if comma < 0:
                break
            text, column = text[comma + 1:], column + comma + 1
            continue
        opened = (number, column)
        field, text, column, last = b"", text[1:], column + 1, False
        while True:  # a quoted field: it may go on over lines
            if (quote := text.find(b'"')) >= 0:
                field, text, column = field + text[:quote], text[quote + 1:], column + quote + 1
                if text.startswith(b'"'):  # `""` is a quote
                    field, text, column = field + b'"', text[1:], column + 1
                elif text.startswith(b","):
                    text, column = text[1:], column + 1
                    break
                elif text in (b"", b"\n"):
                    last = True
                    break
                else:
                    raise parse_error(number, column - 1, AFTER_QUOTE)
            elif text:
                field, column = field + text, column + len(text)
                text, _ = read_line()
                if text:
                    column = 1
            else:  # the value ended inside the quotes
                raise parse_error(*opened, NEVER_CLOSED)
        fields.append(field)
        if last:
            break
    if data[at:].strip(b"\r\n"):
        raise refused("it holds more than one line: give each line's ids in a value of its own")
    return [field.decode() for field in fields]


def _newer(ids: list[str]) -> str:
    n = len(ids)
    return (f"{n} newer proposal{'' if n == 1 else 's'} from the same agent{'' if n == 1 else 's'} still await"
            f"{'s' if n == 1 else ''} review, filed after the first run's list was shown: run cerebro serve --once "
            "to see the list as it stands, or cerebro review")


def _cut_collection(text: str, collections: list[str]) -> tuple[str, str] | None:
    """The collection `text` starts with and the name after it: the
    collection matched without regard to case, the longest first, with no space after its slash; or None."""
    found = ""
    for collection in collections:
        rest = text[len(collection) + 1:]
        if len(text) > len(collection) and text[:len(collection) + 1].casefold() == f"{collection}/".casefold() \
                and len(collection) > len(found) and rest == rest.lstrip():
            found = collection
    return (found, text[len(found) + 1:].strip()) if found else None


def _lower_path(text: str) -> bool:
    """Whether every part of a path is lower case letters, digits, `.`, `_` and `-`, starting with a letter or a
    digit, and it has two parts at least, which reads as a likely typo of a collection's name."""
    def part(p: str) -> bool:
        return bool(p) and (p[0].islower() or p[0].isdecimal()) and all(
            c.islower() or c.isdecimal() or c in "._-" for c in p)
    parts = text.split("/")
    return len(parts) >= 2 and all(part(p) for p in parts)


def _and(items: list[str]) -> str:
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else "".join(items)


def _link_path(text: str) -> str:
    """The path a link names: a leading `./`, `../` or `articles/` and a trailing `.md`
    left out."""
    text = text.strip().removeprefix("./").removeprefix("../")
    return text.removeprefix("articles/").removesuffix(".md")


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\W+", text.casefold()) if w]


def _holding(store: Any, collection: str, name: str) -> list[str]:
    """The articles of `collection` a name finds: the one whose title it is, ignoring case
    and spacing, or whose file name it makes; else each whose title holds every word of it as a whole word."""
    from .records import _slug

    articles = store.query(collection)
    wanted = " ".join(name.casefold().split())
    named = [a.identity.id for a in articles if " ".join(a.identity.title.casefold().split()) == wanted]
    if named:  # a title first, and a file name only when no title is the name
        return named
    named = [a.identity.id for a in articles if a.identity.id == f"{collection}/{_slug(name)}"]
    if named:
        return named
    words = _words(name)
    return [a.identity.id for a in articles if words and all(
        w in _words(a.identity.title) for w in words)]


ONE_PASS = "cerebro serve --once runs one pass now."


def _accepted(told: Any, shown: str, does: str = "carries it out") -> list[str]:
    """The line a command a caller sent reads: accepted, and when its curator acts on it."""
    return [f"accepted  {shown} as {told.proposals[0]}: its curator {does} on its next run. {ONE_PASS}"]


def _admit_article(workspace: Any, article: Any) -> list[str]:
    """An admit of an article that exists: nothing to record when it is in the working set, else the caller's ask
    to bring it back."""
    from .identity import in_working_set

    if in_working_set(article):
        return [f"{article.id} is already in the working set."]
    try:
        told = workspace.tell(YOU, article.title or article.id, target=article.id, kind="article.admit")
    except CerebroError as error:
        raise CerebroError(f"admit {article.id}: {str(error).removeprefix('tell: ')}") from error
    return _accepted(told, article.id)


def _admit_one(workspace: Any, text: str) -> list[str]:
    """Admit what one name names, in this order: an article by its id or path, in any collection; else in
    the collection the text names, an article the name finds; else the caller's ask by name, which the collection's
    curator works out. Gives the lines to print; refused with a line that says what to write instead."""
    from .records import _slug, admit_by_hand

    text, store = text.strip(), workspace.store
    if (path := _link_path(text)) and (found := store.article(path)) is not None:
        return _admit_article(workspace, found.identity)
    curated = sorted({agent.target for agent in workspace.agents if agent.role == "curator"})
    if not curated:
        raise CerebroError("no collection has a curator: add one with cerebro agents new <collection>/curator")
    path = text.removeprefix("articles/")
    if path and not _slug(path):
        raise CerebroError(f"admit {text}: it names nothing an article could be called: give the name, as "
                           f"{curated[0]}/Apollo Launch")
    cut = _cut_collection(path, curated)
    if cut is None:
        first, slash, rest = path.partition("/")
        if slash and first and rest and first == first.strip() and rest == rest.lstrip():
            folders = set(store.collections()) | {agent.target for agent in workspace.agents if agent.target}
            if _lower_path(_link_path(path)) or any(f.casefold() == first.casefold() for f in folders):
                raise CerebroError(f"admit {text}: articles/{first} has no curator, so nothing is admitted there: the "
                                   f"curated collections are {_and(curated)}, and a name with a slash in it takes its "
                                   f"collection first, as {curated[0]}/{path}: cerebro agents new {first}/curator "
                                   "adds a curator")
        if len(curated) > 1:
            example = next((c for c in curated if _holding(store, c, path)), curated[0])
            raise CerebroError(f"admit {text}: name its collection, as {example}/{path}: this workspace curates "
                               f"{_and(curated)}")
        cut = (curated[0], path)
    collection, name = cut
    if not _slug(name):
        raise CerebroError(f"admit {text}: it names nothing an article could be called: give the name, as "
                           f"{collection}/Apollo Launch")
    articles = _holding(store, collection, name)
    if len(articles) > 1:
        each = "; ".join(f"{id} {store.article(id).identity.title}" for id in articles)
        raise CerebroError(f"admit {text}: {len(articles)} articles answer to {name}, so none is admitted: admit one "
                           f"by its id: {each}")
    if articles:
        return _admit_article(workspace, store.article(articles[0]).identity)
    told = workspace.masked(admit_by_hand(store, workspace.extraction, YOU, collection, name, clock=workspace.clock))
    return _accepted(told, f"{collection}/{name.strip()}")


@main.command()
@click.argument("names", nargs=-1)
@click.option("--from", "from_", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="A file of names to admit, one a line.")
@_root_option
@_refusing
def admit(names: tuple[str, ...], from_: Path | None, root: Path | None) -> None:
    """Add an article to the working set, as <collection>/<name>, quoting a name with spaces, or by an article's id
    or path. A bare name goes to the one curated collection. It is your decision, so it is accepted as you send it,
    and the collection's curator finds what the name means and makes the article on its next run. Each name is
    admitted on its own."""
    given = [*names, *([line.strip() for line in from_.read_text().splitlines() if line.strip()] if from_ else [])]
    if not given:
        raise click.UsageError("admit needs a name, or --from <file> with one a line: cerebro admit "
                               "<collection>/<name>")
    failed: list[str] = []
    usage = False
    with open_workspace(root) as workspace:
        for text in given:
            try:
                for line in _admit_one(workspace, text):
                    _echo(line)
            except CerebroError as error:
                failed.append(shown(" ".join(str(error).split())))
                usage = usage or isinstance(error, _Usage)
    if failed:
        refused = click.ClickException("\n".join(failed))
        refused.exit_code = 2 if usage else 1  # a name to give more exactly exits 2
        raise refused


def _archived_one(store: Any, text: str) -> Any:
    """The article `text` names: by its id or path, a merged one by the article
    it was merged into, else by its title or an alias, ignoring case and spacing. Refused when none answers, or more
    than one."""
    from .identity import follow, redirects

    if (found := store.article(_link_path(text))) is not None:
        if found.identity.status == "merged":
            found = store.article(follow(found.identity.id, redirects(store))) or found
        return found.identity
    wanted = " ".join(text.casefold().split())
    named = [a.identity for c in store.collections() for a in store.query(c) if a.identity.status != "merged" and any(
        " ".join(n.casefold().split()) == wanted for n in [a.identity.title, *a.identity.keys.get("alias", [])])]
    if not named:
        raise CerebroError(f'archive {text}: there is no article "{text}": give its path, as <collection>/<name>, '
                           "or its title")
    if len(named) > 1:
        raise CerebroError(f'archive {text}: "{text}" names {" and ".join(sorted(p.id for p in named))}: give one by '
                           "its path")
    return named[0]


@main.command()
@click.argument("names", nargs=-1)
@_root_option
@_refusing
def archive(names: tuple[str, ...], root: Path | None) -> None:
    """Take an article out of the working set, by its path, as <collection>/<name>, or by its title. It is your
    decision, so it is accepted as you send it, and the collection's curator archives it on its next run: then a
    query leaves it out and no agent writes it. Its file and what it has stay. cerebro admit brings it back."""
    from .identity import in_working_set
    from .records import archive_by_hand

    text = " ".join(names).strip()
    if not text:
        raise click.UsageError("archive needs an article: cerebro archive <collection>/<name>")
    with open_workspace(root) as workspace:
        article = _archived_one(workspace.store, text)
        if not in_working_set(article):
            _echo(f"{article.id} is already out of the working set.")
            return
        told = workspace.masked(archive_by_hand(workspace.store, workspace.extraction, YOU, article.id,
                                                clock=workspace.clock, root=workspace.root))
    for line in _accepted(told, article.id, "archives it"):
        _echo(line)


@main.command()
@click.option("--article", "article", default=None, help="The article's id, such as reports/weekly.")
@_root_option
@click.option("--dry-run", "dry_run", is_flag=True,
              help="Print what compile --article would do on the one named, or with no --article what the next pass, "
                   "serve --once, would run, and run and write nothing.")
@_refusing
def compile(article: str | None, root: Path | None, dry_run: bool) -> None:
    """Rebuild one article now, with the agent that writes it, or each article of a collection, when what it reads
    moved. The article is named by its id or its path under articles/, a collection by its folder."""
    if article is not None and not article:
        raise click.UsageError("Invalid value for '--article': give an article's id, its path under articles/, or a "
                               "collection's folder")
    if dry_run:
        with open_workspace(root) as workspace:
            if article and (why := _out_of_working_set(workspace, article)):
                _echo(f"Nothing to run. {why}.")  # as compile --article says it
                return
            refused, report = _refused_files(workspace, lambda: preview.stale(workspace, article or ""), said=not article)
        for line in preview.stale_lines(report):
            _echo(line)
        _failed_runs(report.count("failed"), len(refused))
        return
    if not article:
        raise click.UsageError("Missing option '--article'.")
    with open_workspace(root) as workspace:
        if why := _out_of_working_set(workspace, article):
            _echo(f"Nothing to run. {why}.")  # said before a key is asked for, since no model would run
            return
        refused, picked = _refused_files(workspace, lambda: preview.chosen(workspace, workspace.lookup, article))
        for agent, _ in picked.jobs:  # a missing key refuses the whole command in one line, before any run
            workspace.need_key(agent)
        counts = dict.fromkeys(("written", "ran", "fresh", "failed", "waiting", "alone"), 0)
        capped: list[str] = []  # the cap's own line, once a daily cap stopped a run
        paid = False  # whether any run of this command was paid for, or wrote
        for agent, articles in picked.jobs:
            with contextlib.ExitStack() as held:
                try:
                    held.enter_context(workspace.hold(agent))
                    for line in workspace.ready_by_hand(agent, articles):
                        _echo(f"warning  {line}")
                except Exception as error:  # noqa: BLE001 - each of its articles is a failed line, and the rest go on
                    for article_id in articles:
                        counts[_by_hand_failed(workspace, agent, article_id, error)] += 1
                    continue
                for article_id in articles:  # one article that fails or waits is its own line, and the others go on
                    try:
                        compiled = workspace.compile(agent, this=article_id, scheduled=True, by_hand=True)  # though fresh
                    except BudgetSpent as error:  # the cap refused its model call: it waits
                        counts[_capped(workspace, agent, article_id, preview.said(workspace, error), capped)] += 1
                        continue
                    except Exception as error:  # noqa: BLE001
                        counts[_by_hand_failed(workspace, agent, article_id, error)] += 1
                        continue
                    counts[_by_hand(workspace, agent, article_id, compiled, capped)] += 1
                    paid = paid or compiled.article is not None or any(run.id for run in compiled.runs)
        for id in picked.archived:
            _echo(f"{id} is archived, left alone")
        if picked.collection:
            ran = f"{counts['ran']} ran and wrote nothing, " if counts["ran"] else ""
            waits = f", {counts['waiting']} waiting" if counts["waiting"] else ""
            _echo(f"{counts['written']} written, {ran}{counts['fresh']} fresh, {counts['failed']} failed{waits}.")
        if capped:
            _echo(capped[0])
        _failed_runs(counts["failed"], len(refused))
        if capped and not paid:  # nothing was done: the cap is the fix, and the command failed
            raise SystemExit(1)


def _out_of_working_set(workspace: Cerebro, named: str) -> str:
    """Why `compile --article` runs nothing on the one article `named`, archived or merged away, or "" when it names a
    collection or an article in the working set."""
    one = named.removeprefix("articles/").removesuffix(".md")
    if "/" not in one.strip("/") or workspace.writer_of(one) is None:
        return ""
    found = workspace.store.article(one)
    return out_of_working_set(one, found.identity if found is not None else None)


def _refused_files(workspace: Cerebro, then: Callable[[], Any], said: bool = False) -> tuple[list[str], Any]:
    """Print each agent file that did not load as a `refused` line, after the line that says the dry run stands for
    the next pass when `said`, and give them with what `then` gives. When `then` refuses the command, its one line
    is all that is said."""
    if said:
        _echo(preview.NEXT_PASS)
    refused = [workspace.mask_every()(line) for line in workspace.refused if line.startswith("agents/")]
    out = then()
    for line in refused:
        _echo(f"refused  {line}")
    return refused, out


def _failed_runs(failed: int, refused: int) -> None:
    """Exit 1 naming what failed and what was refused, each told on its own line above, when either is not 0."""
    said = [f"{_count(failed, 'agent run')} failed"] * bool(failed) + [f"{_count(refused, 'agent file')} refused"] * \
        bool(refused)
    if said:
        raise click.ClickException(f"{' and '.join(said)}: see {'its line' if failed + refused == 1 else 'their lines'} "
                                   "above")


def _capped(workspace: Cerebro, agent: Agent, article: str, line: str, capped: list[str]) -> str:
    """Print that a daily cap stopped one run of `compile --article`, keep the cap's own
    `line` to say once after the runs, and count it waiting."""
    _echo(f"waiting  {article}  {agent.id}: {preview.cap_words(line)}")
    if not capped:
        capped.append(line)
    return "waiting"


def _by_hand(workspace: Cerebro, agent: Agent, article: str, compiled: Compiled, capped: list[str]) -> str:
    """Print what `compile --article` did with one run, and give how it is
    counted: written, ran (it wrote nothing), fresh, waiting or failed."""
    from .run import WROTE_NOTHING
    from .runner import unmoved

    if compiled.left_alone:  # a person took it out of the working set while it ran, or before: said as agents run says it
        _print_compiled(agent.id, article, compiled, workspace=workspace)
        return "ran" if compiled.runs else "alone"
    if compiled.skipped and compiled.waiting:
        _echo(f"waiting  {article}  {agent.id}: {compiled.waiting}")
        return "waiting"
    if compiled.skipped:  # nothing it reads moved, as the dry run says
        _echo(f"fresh    {article}  {unmoved(workspace, agent, [article])}")
        return "fresh"
    if compiled.article is not None:
        _echo(f"wrote    {article}  articles/{article}.md")
        if compiled.warning:
            _echo(f"warning  {compiled.warning}")
        return "written"
    run = compiled.run
    if run is not None and run.status == "budget spent" and not run.id:  # a daily cap refused it before it ran
        return _capped(workspace, agent, article, workspace.mask_every()(" ".join(run.note.split())), capped)
    if run is not None and run.status == WROTE_NOTHING:
        key = agent.id if article == agent.id else f"{agent.id} on {article}"  # as agents run says it
        _echo(f"ran      {key}: wrote nothing: {run.note or 'it handed in nothing'}")
        return "ran"
    why = f"run {run.id} wrote nothing: {run.status}" + (f": {run.note}" if run.note else "") if run else "no run"
    _echo(f"failed   {article}  {agent.id}: {why}")
    return "failed"


def _by_hand_failed(workspace: Cerebro, agent: Agent, article: str, error: Exception) -> str:
    why = error.message if isinstance(error, click.ClickException) else preview.said(workspace, error)
    _echo(f"failed   {article}  {agent.id}: {workspace.mask_every()(' '.join(why.split()))}")
    return "failed"


# -- prompts --------------------------------------------------------------------------------------------------------

@main.group()
def prompts() -> None:
    """The bundled prompts a run wraps around an agent's own words."""


@prompts.command("list")
@_root_option
@_json_option
@_refusing
def prompts_list(root: Path | None, as_json: bool) -> None:
    """The bundled prompts, which of them this workspace ejected into prompts/ to edit, and each file there that no
    bundled prompt is named after, which nothing reads."""
    from . import prompts as bundled

    _checked_workspace(root)
    data = [{**row, "bundled": True} for row in scaffold.prompts(root or Path.cwd())]
    unread = [{"name": Path(file).stem, "ejected": False, "bundled": False, "path": f"prompts/{file}"}
              for file in bundled.unread((root or Path.cwd()).resolve())]
    if as_json:
        _print_json(data + unread)
        return
    for row in data:
        said = f"ejected, read from {row['path']}" if row["ejected"] else "bundled"
        _echo(f"{row['name']}  {said}")
    for row in unread:
        _echo(f"{row['name']}  {row['path']}, read by nothing: no bundled prompt has this name")
    _echo(f"{len(data)} prompts, {sum(row['ejected'] for row in data)} ejected"
          + (f", {_count(len(unread), 'file')} read by nothing." if unread else "."))


@prompts.command("eject")
@click.argument("name")
@_root_option
@_refusing
def prompts_eject(name: str, root: Path | None) -> None:
    """Copy the bundled prompt NAME into prompts/NAME.md, to edit. Runs read the copy from then on."""
    _checked_workspace(root)
    made = scaffold.eject_prompt(root or Path.cwd(), name)
    _echo(f"wrote    {made.path}")
    _echo(f"next     {made.next}")


# -- sources and tools ----------------------------------------------------------------------------------------------

@main.group()
def sources() -> None:
    """The sources of the workspace."""


@sources.command("new")
@click.argument("name")
@_root_option
@click.option("--glob", "globs", multiple=True, help="Files to read, relative to the workspace. Repeat for more.")
@click.option("--extract", default="facts", help="What the records extract into. The default is facts.")
@_refusing
def sources_new(name: str, root: Path | None, globs: tuple[str, ...], extract: str) -> None:
    """Scaffold sources/NAME.yaml: a folder of files, one record per file. With no --glob, it reads NAME/**/*.md."""
    _checked_workspace(root)
    made = scaffold.new_source(root or Path.cwd(), name, globs, extract)
    _echo(f"wrote    {made.path}")
    _echo(f"next     {made.next.removesuffix('cerebro serve --once')}cerebro sources check {name}")


@sources.command("check")
@click.argument("name")
@_root_option
@click.option("--all", "whole", is_flag=True,
              help="List the source's whole window, not only what changed since the cursor the last pass kept.")
@click.option("--limit", type=click.IntRange(min=0), default=0,
              help="Stop after getting this many records. The default, 0, gets every one.")
@_refusing
def sources_check(name: str, root: Path | None, whole: bool, limit: int) -> None:
    """List what source NAME holds for the next pass and get each record, with no model, and print what it found and
    how many wait. It keeps nothing: the next pass lists the same records."""
    from .sources_check import check

    with open_workspace(root) as workspace:
        workspace.refresh_mask()
        _echo(workspace.mask_every()(f"workspace {workspace.root}"), cap=None)
        checked = check(workspace, name, whole=whole, limit=limit)
    for line in checked.lines():
        _echo(line)
    if checked.failure or checked.failed:
        raise SystemExit(1)


@main.command()
@click.argument("name")
@click.option("--json", "arguments", default="{}", show_default=True,
              help="The tool's arguments as a JSON object, as an outside agent sends them.")
@_root_option
@_refusing
def tools(name: str, arguments: str, root: Path | None) -> None:
    """Call one MCP tool, such as read or query, as you, the workspace's owner, and print its data as JSON: a tell or
    decide made here is yours, as any caller's is."""
    from .mcp import UnknownArgument, read_arguments

    try:
        args = read_arguments(arguments)
    except json.JSONDecodeError as error:
        raise click.ClickException(f"--json is not JSON ({error.msg} at character {error.pos}): quote it, as "
                                   "'{\"ids\": [\"reports/weekly\"]}'") from None
    except UnknownArgument as error:
        raise click.ClickException(str(error)) from None
    if not isinstance(args, dict):
        raise click.ClickException("--json is not a JSON object: write the arguments as {\"key\": value}")

    with open_workspace(root) as workspace:
        try:  # the arguments checked as the MCP server checks an outside agent's, with the same line
            args = workspace.mcp_server().checked(name, args)
        except UnknownArgument as error:
            raise click.ClickException(str(error)) from None
        _print_json(workspace.call(YOU, name, args))


# -- secrets -------------------------------------------------------------------------------------------------------

def _no_such_option(ctx: click.Context, error: click.NoSuchOption) -> None:
    """An option a secrets command does not take is not shown, since a value pasted there may start with -."""
    command = ctx.command_path.split(" ", 1)[-1]
    error.message = f"{command} has no option by that name, so it is not shown: {command} --help lists its options"
    error.possibilities = []


class _Unechoed(_Leaf):
    """A secrets command: a word typed on its line that it does not take, which may be a value pasted there, after
    `--` or with a - before it too, is not shown: the line says what the command takes. Every other usage error, such
    as a file given to --workspace, keeps its own reason."""

    takes = ""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.NoSuchOption as error:
            _no_such_option(ctx, error)
            raise
        except click.UsageError as error:
            if type(error) is click.UsageError and error.message.startswith("Got unexpected extra argument"):
                error.message = (f"{ctx.command_path.split(' ', 1)[-1]} takes {self.takes}, so what else was typed "
                                 "on its line is not shown: a value is typed at the prompt, or piped in")
            raise


class _SecretsGroup(_Group):
    """The secrets group: a word that is no command of it is not shown, since it may be a value pasted there."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.NoSuchOption as error:
            _no_such_option(ctx, error)
            raise

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple[str | None, click.Command | None,
                                                                            list[str]]:
        name = click.utils.make_str(args[0]) if args else ""
        if name and self.get_command(ctx, name) is None and not ctx.resilient_parsing:  # completion asks no refusal
            ctx.fail("secrets has no command by that name, so it is not shown: its commands are set, list and unset")
        return super().resolve_command(ctx, args)


class _Folder(click.Path):
    """The folder a secrets command acts on: a file named there is refused in one line, exit 1."""

    def __init__(self) -> None:
        super().__init__(path_type=Path, file_okay=False)

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if os.path.isfile(value):
            raise click.ClickException(f"--workspace {shown_path(Path(value).resolve())}: a file, not a folder")
        return super().convert(value, param, ctx)


def _secrets_workspace(fn: Callable[..., Any]) -> Callable[..., Any]:
    return click.option("--workspace", "root", type=_Folder(), default=None, callback=_named_before,
                        help="The workspace folder. The default is the current folder.")(fn)


def _name_given(name: str) -> None:
    """A secret's name checked: one that cannot be a name is a usage error, exit 2."""
    from .secrets import check_name

    try:
        check_name(name)
    except CerebroError as error:
        refused = click.ClickException(" ".join(str(error).split()))
        refused.exit_code = 2
        raise refused from None


def _typed_limit() -> int | None:
    """The most bytes a line typed at stdin's terminal holds before the terminal cuts it, its newline left out:
    Linux's line discipline holds 4,096 bytes, which fpathconf there misstates as 255; elsewhere, as on macOS, the
    system's own MAX_CANON says. None when it cannot be known."""
    if sys.platform.startswith("linux"):
        return 4095
    try:
        return int(os.fpathconf(sys.stdin.fileno(), "PC_MAX_CANON")) - 1
    except (OSError, ValueError, AttributeError):
        return None


@main.group(cls=_SecretsGroup, invoke_without_command=True)
@_secrets_workspace
@click.option("--plain", is_flag=True, help="Show the keys as a table and ask on plain lines: no lobby is drawn.")
@click.pass_context
@_refusing
def secrets(ctx: click.Context, root: Path | None, plain: bool) -> None:
    """The workspace's secrets store: model keys and source logins, kept outside the workspace's folder.

    Alone, it shows the model keys the workspace's jobs use and whether each is set: at a terminal, the keys lobby
    init shows, where Enter opens a provider and takes its key, typed hidden, proven with one small call and kept;
    at a terminal that draws nothing, the keys as a table, then the names to set; off a terminal, the table, and it
    exits 2. Outside a workspace it prints this help."""
    if ctx.invoked_subcommand is not None:  # it acts on the workspace named here, unless it names its own
        return
    if root is not None and not root.exists():  # a folder named and not there is refused
        raise click.ClickException(f"--workspace {shown_path(root)}: no such folder")
    target = (root or Path.cwd()).resolve()
    if not (target / "settings.yaml").is_file():
        click.echo(ctx.get_help())
        return
    _keys_alone(ctx, target, plain)


def _key_session(root: Path, screen: Any, mark: Any = None) -> Any:
    """The keys lobby's session on the workspace at `root`, as init's walk has it: the rows read afresh from the
    models and the store, each key proven by `_prove_on(root)` and kept."""
    from . import guided

    walk = guided.Walk(root, screen, open_workspace, lobby=lambda: None, first_run=lambda: None, home=Path(),
                       command="cerebro", prove=_prove_on(root), mark=mark)
    return walk._key_session()


def _keys_alone(ctx: click.Context, root: Path, plain: bool) -> None:
    """`secrets` alone: the lobby at a terminal that draws, the table and a line of
    names at one that does not, and off a terminal the table, then exit 2 naming secrets set. At a terminal it ends
    with a line per provider still with no key, how many keys are set, and what to run next."""
    from . import guided, keys
    from .output import Printer

    if not _asking():
        with open_workspace(root) as workspace:
            kept = workspace.secrets.names() if workspace.secrets is not None else []
            models = dict(workspace.models)
        rows = keys.key_rows(models, kept, os.environ)
        for line in keys.table(rows):
            _echo(line)
        names = ", ".join(r.provider for r in rows)
        _echo(f"Error: secrets alone asks for the model keys at a terminal: set one with cerebro secrets set "
              f"<provider>, for {names}", err=True)
        ctx.exit(2)
    printer = _drawing(plain)
    stopped = False
    try:
        if printer is not None:
            screen = guided.TerminalScreen(printer, _echo)
            session = _key_session(root, screen, printer.mark)
            with screen.step_apart() if screen.drawn else contextlib.nullcontext():  # set apart
                session.lobby(keys.head())
        else:
            printer = Printer(plain=True)
            session = _key_session(root, guided.TerminalScreen(None, _echo))
            session.plain(_echo)
    except KeyboardInterrupt:  # Ctrl-C between one prompt and the next: what stands is said, then exit 130
        _echo("")
        stopped = True
    with keys.held_ctrl_c():  # the count is said whole, never cut by a late Ctrl-C into Aborted!
        rows = session.read()
        said, then = keys.summary(rows)
        for line in session.checks():
            printer.check(line)
        _echo(said)
        if stopped:
            raise click.exceptions.Exit(130)
        printer.next(then)
        click.echo(printer.last_line(), err=True)


@secrets.command("set", cls=type("_Set", (_Unechoed,), {"takes": "one name"}))
@click.argument("name")
@_secrets_workspace
@_refusing
def secrets_set(name: str, root: Path | None) -> None:
    """Keep a secret under NAME, such as fireworks for its model key, read from a hidden prompt or standard input."""
    from .keys import control, typed
    from .models import PROVIDERS

    if "=" in name:  # NAME=value typed in one go: the value is not shown, a usage error
        raise click.UsageError("secrets set takes the name alone, so what follows = is not shown: type the value at "
                               "the prompt, or pipe it in", click.get_current_context())
    _name_given(name)
    # A provider's key is kept under its id, so Fireworks typed by hand lands where the jobs look.
    name = next((provider for provider in PROVIDERS if provider.casefold() == name.casefold()), name)
    with open_workspace(root) as workspace:
        if workspace.secrets is None:
            raise click.ClickException("settings.yaml has no id, so no secrets store is open: add a line such as id: "
                                       "notes-wiki to settings.yaml, then run this again")
        if _asking():
            from .guided import held_terminal

            with held_terminal():  # the modes come back on a signal, and Ctrl-\ and Ctrl-Z are keys
                line = click.prompt(f"Value of {name}", hide_input=True)
            limit = _typed_limit()
            if limit is not None and len(line.encode()) >= limit:  # the terminal may have cut it: nothing is kept
                raise click.ClickException(f"the value of {name} fills the prompt's {limit} characters and may be "
                                           "cut, so nothing was stored: pipe it in instead")
            value, escaped = typed(line)
            if escaped:  # Escape at the hidden prompt: cancelled, the old key kept; an arrow in a key is left out
                raise click.ClickException(f"no value for {name}: cancelled at the prompt, nothing stored")
            value = value.strip()  # as a piped value and the lobby's key are
        else:
            value = sys.stdin.read().strip()
        if not value:
            refused = click.ClickException(f"no value was given for {name}: type it at the prompt, or pipe it in")
            refused.exit_code = 2  # a usage error
            raise refused
        if control(value):
            raise click.ClickException(f"the value for {name} holds a control character: nothing stored, so type "
                                       "or paste the key alone")
        workspace.secrets.set(name, value)
    _echo(f"kept     {name} in the workspace's secrets store")


@secrets.command("list", cls=type("_List", (_Unechoed,), {"takes": "no name"}))
@_secrets_workspace
@_refusing
def secrets_list(root: Path | None) -> None:
    """Print the names of the secrets kept for the workspace, and where they are kept. Never a value."""
    with open_workspace(root) as workspace:
        if workspace.secrets is None:
            raise click.ClickException("settings.yaml has no id, so no secrets store is open: add a line such as id: "
                                       "notes-wiki to settings.yaml, then run this again")
        names, where = workspace.secrets.names(), workspace.secrets.where()
        others, unread = _other_stores(workspace)
    for name in names:
        _echo(f"secret   {name}")
    _echo(f"kept     {_count(len(names), 'secret')} in {where}")
    for other in others:
        _echo(f"also     {', '.join(other.names())} in {other.where()}, which this workspace does not read now: "
              "cerebro secrets unset removes a name from every store")
    for line in unread:
        _echo(f"unread   {line}")


def _other_stores(workspace: Cerebro) -> tuple[list[Any], list[str]]:
    """The stores other than the one the workspace reads now that hold a secret of it, and a line for each store
    that cannot be read here, since `CEREBRO_SECRETS_STORE` may have picked another store when a secret was kept."""
    from .secrets import NO_KEYCHAIN, every_store

    stores, unread = every_store(str(workspace.settings["id"]))
    here = workspace.secrets.where() if workspace.secrets is not None else ""
    return [s for s in stores if s.where() != here and s.names()], [u for u in unread if u != NO_KEYCHAIN]


@secrets.command("unset", cls=type("_Unset", (_Unechoed,), {"takes": "one name"}))
@click.argument("name")
@_secrets_workspace
@_refusing
def secrets_unset(name: str, root: Path | None) -> None:
    """Remove the secret NAME from the workspace's secrets store. Removing one that is not there is no error."""
    _name_given(name)
    with open_workspace(root) as workspace:
        if workspace.secrets is None:
            raise click.ClickException("settings.yaml has no id, so no secrets store is open: there is nothing to "
                                       "unset")
        others, unread = _other_stores(workspace)
        removed = [s.where() for s in [workspace.secrets, *others] if s.delete(name)]
    for where in removed:
        _echo(f"unset    {name} in {where}")
    if not removed:
        _echo(f"unset    {name} was not set, so nothing was removed")
    for line in unread:
        _echo(f"unread   {line}")


# -- forget ---------------------------------------------------------------------------------------------------------

def _outside_lines(out: Outside) -> list[str]:
    """What forget would remove for one workspace, a line each, by name and never by value."""
    lines = [f"secrets  {', '.join(names) or 'a list of names with no secrets'} in {where}" for where, names in
             out.secrets]
    lines += [f"unread   {line}" for line in out.unread]
    if out.others:
        lines.append(f"keep     the secrets of id {out.id}: {' and '.join(out.others)} "
                     f"{'holds' if len(out.others) == 1 else 'hold'} the same id")
    if out.claim:
        lines.append(f"claim    id {out.id} for {out.claim}, in {claims_file()}")
    if out.trusted:
        lines.append(f"trust    the record of {out.folder}, in {trusting.trusted_path()}")
    return lines


def _forget_workspace(root: Path, yes: bool) -> None:
    real = root.resolve()
    out = of_workspace(real)
    named = f"id {shown(out.id, trusting.CAP)}" if out.id else "no id in settings.yaml"
    _echo(f"folder   {real}, {named}", cap=None)
    lines = _outside_lines(out)
    for line in lines:
        _echo(line)
    if out.empty():
        _echo("nothing  this workspace keeps nothing outside its folder that forget removes")
    elif not yes:
        if not _asking():
            raise click.ClickException("nothing removed: forget asks before it removes anything, and there is no "
                                       "terminal to ask at: run it at a terminal, or add --yes")
        if not click.confirm(shown_path(f"Forget the workspace at {real}? Its secrets cannot be got back."),
                             default=False):
            _echo("nothing  removed")
            return
    for line in [] if out.empty() else remove(out):
        _echo(line)
    name = scaffold.mcp_entry(real, "")["name"]
    _echo(f"left     the folder {real} and its files stay as they were", cap=None)
    _echo(f"left     an MCP client entry you added from the line init printed: run claude mcp remove --scope user "
          f"{name} for Claude Code, or delete {name} under mcpServers in Claude Desktop's or Cursor's config")


def _forget_leftovers(folder: Path | None, yes: bool) -> None:
    found = left_behind()
    if folder is not None:
        found = [out for out in found if out.folder == str(folder)]
        if not found:
            raise _Whole(f"nothing is left behind by a workspace at {folder}: run cerebro forget with no "
                         "folder to list what deleted workspaces left")
    if not found:
        _echo("nothing  no workspace whose folder is gone left anything behind")
        return
    _echo(f"left     {_count(len(found), 'workspace')} no longer at {'its' if len(found) == 1 else 'their'} recorded "
          "folder left things behind")
    asking = _asking() and not yes
    for out in found:
        named = shown(out.id, trusting.CAP)
        if not out.folder:
            where = f"id {named}, folder unknown"
        elif there(out.folder):
            where = f"id {named}, recorded at {out.folder}, which now holds another id"
        else:
            where = (f"id {named}, " if out.id else "") + f"recorded at {out.folder}, which is gone"
        _echo(f"folder   {where}", cap=None)
        for line in _outside_lines(out):
            _echo(f"  {line}")
        if asking:
            if click.confirm(shown("Remove what it left?"), default=False):
                for line in remove(out):
                    _echo(f"  {line}")
            else:
                _echo("  kept     nothing removed")
    if not asking:
        _echo("kept     nothing removed: cerebro cannot tell a folder that moved from one that was deleted, so what a "
              "workspace left goes only on a yes typed at a terminal. If a workspace moved, run cerebro trust and then "
              "cerebro status in its folder to record where it is now. If it was deleted, run cerebro forget at a "
              "terminal and answer yes")


@main.command("forget")
@click.argument("folder", required=False, type=click.Path(path_type=Path, file_okay=False))
@_root_option
@click.option("--yes", is_flag=True, help="Remove without asking, but for what a workspace whose folder is gone left.")
@_refusing
def forget_command(folder: Path | None, root: Path | None, yes: bool) -> None:
    """Remove what the workspace in FOLDER, or the current folder, keeps outside it: its secrets, its claim on its id
    and its trust record. The folder stays. It lists each by name and asks first, and --yes removes without asking.
    With no workspace in the folder it runs in, or a FOLDER that is gone, it lists what deleted workspaces left
    behind, each with its folder and id, and removes each only on a yes typed at a terminal. Trust is not needed."""
    if folder is not None and root is not None and folder.resolve() != root.resolve():
        raise _Whole(f"forget was given two folders, {folder} and --workspace {root}: give one")
    target = folder or root
    if target is None:
        here = Path.cwd()
        if (here / "settings.yaml").is_file():
            _checked_workspace(here, trust=False)
            _forget_workspace(here, yes)
        else:
            _forget_leftovers(None, yes)
        return
    target = target.resolve()
    if (target / "settings.yaml").is_file():
        _checked_workspace(target, trust=False)
        _forget_workspace(target, yes)
    elif there(target):
        raise click.ClickException(f"{target} is not a workspace (no settings.yaml in it), so forget removes "
                                   "nothing: name the folder that holds settings.yaml")
    else:
        _forget_leftovers(target, yes)


def _drawing(plain: bool) -> Any:
    """The printer a screen draws with, on a terminal that takes styles and keys, or None: with --plain, NO_COLOR,
    TERM=dumb, or a stdin, stdout or stderr that is no terminal, a command prints plain lines. A list draws on
    stderr and reads stdin."""
    if plain or not sys.stdout.isatty() or not _asking() or not sys.stderr.isatty() or os.environ.get("NO_COLOR") \
            or os.environ.get("TERM") == "dumb":
        return None
    from .output import Printer

    printer = Printer()
    return printer if printer.styled_out else None


INTERRUPTED = 130  # the exit of a command Ctrl-C ended
_ONE_SOURCE = ("from_stdin", "from_env", "replace_kept")  # flags that are about one source, so it must be named


class _SourceOrLobby(click.Argument):
    """`connect`'s SOURCE, declared required, so its usage and its error read as a required argument's. Its absence,
    with no flag that is about one source, is the command's own to answer: the lobby at a terminal that draws, else
    the same usage error, then the workspace's sources."""

    def process_value(self, ctx: click.Context, value: Any) -> Any:
        if self.value_is_missing(value) and not any(ctx.params.get(f) for f in _ONE_SOURCE):
            return None
        return super().process_value(ctx, value)


class _NoSource(click.MissingParameter):
    """`connect` with no SOURCE off a terminal: the workspace's sources, each with where it
    stands and what it needs, on stdout, then a usage line naming the sources, and the failed line, exit 2."""

    def __init__(self, ctx: click.Context, param: click.Parameter, table: list[str], names: list[str],
                 plain: bool = False):
        super().__init__(ctx=ctx, param=param)
        self.table = table
        self.names = names
        self.plain = plain

    def show(self, file: Any = None) -> None:
        for line in self.table:
            click.echo(line)
        names = f", one of {', '.join(shown(n, 120) for n in self.names)}" if self.names else ""
        click.echo(f"Error: connect needs a source's name{names}: run it in a terminal to be asked, or pass <name>",
                   err=True)
        click.echo(_failed_line("cerebro connect <name>", self.plain), err=True)


def _rows(root: Path | None, failed: dict[str, str] | None = None) -> list[Any]:
    """The lobby's rows for the workspace at `root`. A workspace that does not open raises what it raises."""
    from .connect import places

    with open_workspace(root) as workspace:
        return places(workspace, failed)


def _places(root: Path | None) -> list[Any]:
    """The lobby's rows for the workspace at `root`, or none where no workspace opens, for the usage error: the
    lobby itself says why a workspace does not open."""
    try:
        return _rows(root)
    except (CerebroError, click.ClickException, OSError):
        return []


def _table(rows: list[Any], root: Path | None = None) -> list[str]:
    """The lobby's rows as plain lines: each source by name, where it stands, and why it needs attention, with the
    command that fixes it, or what it needs."""
    from .connect import connecting
    from .connect_screen import ATTENTION, NOT_ADDED
    from .source import SourceError, read_source

    def why(p: Any) -> str:  # a reason that names its own command needs no second one
        said = " ".join(p.reason.split())
        names = "Run: " in said or "run cerebro " in said.casefold()
        return shown(said if names else f"{said} Run: cerebro connect {p.name}", 300)

    def needs(p: Any) -> str:  # what its kind needs
        kind = p.name
        if p.state != NOT_ADDED:
            try:
                kind = read_source((root if root is not None else Path.cwd()).resolve() / "sources" /
                                   f"{p.name}.yaml").kind
            except (SourceError, OSError):
                return ""
        said = connecting(kind)
        return said.needs if said is not None else ""

    cells = [(shown(p.name, 120), p.state, why(p) if p.state == ATTENTION else needs(p)) for p in rows]
    first = max((len(c[0]) for c in cells), default=0) + 2
    second = max((len(c[1]) for c in cells), default=0) + 2
    return [f"{name:<{first}}{state:<{second}}{what}".rstrip() for name, state, what in cells]


def _typed(line: str, hidden: bool = False, err: bool = False, suffix: str = "") -> str:
    """A person's answer at a plain prompt. Input that ends raises EOFError, and Ctrl-C raises KeyboardInterrupt,
    which ends connect: exit 130, with nothing printed and nothing kept."""
    try:
        return str(click.prompt(line, hide_input=hidden, default="", show_default=False, prompt_suffix=suffix,
                                err=err))
    except click.Abort as stop:  # click reads Ctrl-C and input that ends alike, and keeps which as the context
        if isinstance(stop.__context__, KeyboardInterrupt):
            raise KeyboardInterrupt from None
        if not hidden:  # the line the prompt left open ends before what follows is said
            click.echo("", err=err)
        raise EOFError from None


def _answer(line: str) -> str:
    """A person's answer on a plain line, or EOFError when input ends."""
    return _typed(line)


class _Tell:
    """What a source says before it asks and the choice it asks first, shown on stderr where connect's prompts are:
    drawn by `printer` where the terminal draws, else as plain lines, the choice asked on a plain line. `apart` is the
    alternate screen, the lobby's when it opened the view: a choice is drawn there from the top, and a choice of
    where to look keeps it for the questions after it, which Escape at a prompt comes back from. `close` leaves the
    screen apart once connect ends, when the view took it."""

    def __init__(self, printer: Any, apart: Any = None, title: str = "") -> None:
        from .connect_screen import Apart
        from .output import Printer

        self.title = title
        self.drawn = printer is not None and printer.styled_out
        self.printer = printer if printer is not None else Printer(plain=True)
        self.lobby = apart is not None and apart.on
        self.apart = apart if apart is not None else Apart(printer if self.drawn else None)
        # whether the view's title heads it yet: the lobby heads each view it opens; `connect <source>` alone heads
        # its own, so every road shows the same view
        self.headed = self.lobby or not self.title

    def explain(self, lines: Any, block: str = "", after: Any = ()) -> None:
        import shutil

        from .connect_screen import explained
        from .output import visible_width

        said = explained(self.printer, lines, block, after)
        if self.drawn and not self.headed:
            click.echo(self.printer.bold(self.title), err=True)
            self.headed = True
        if self.lobby and self.apart.on:  # a view taller than the screen apart, which keeps no scrollback, is drawn
            cols = max(shutil.get_terminal_size().columns, 1)  # on the main screen, to scroll and copy whole
            rows = 1 + sum(max(1, -(-visible_width(line) // cols)) for line in said) + 3  # title, lines, prompt
            if not self.apart.fits(rows):
                self.apart.leave()
                click.echo("", err=True)
                click.echo(self.printer.bold(self.title), err=True)
        for line in said:
            click.echo(line, err=True)

    def head(self, title: str) -> None:
        if not self.drawn:
            return
        if self.apart.on:  # a view apart draws from the top of a clear screen, its title first
            self.apart.take()
        else:  # a view heads a screen: a blank line, then the title
            click.echo("", err=True)
        click.echo(self.printer.bold(title), err=True)
        self.headed = True

    def choose(self, title: str, lines: Any, ways: Any) -> int | None:
        from .checklist import MENU_DONE, choose
        from .connect_screen import plain_ways, way_of, ways_menu

        if self.drawn:
            self.apart.take()
            self.headed = True  # the choice is headed by its title
            if lines:  # its lines end in a blank line
                click.echo(self.printer.bold(title), err=True)
                self.explain(lines)
            got = choose(ways_menu(title, ways, lines), self.printer)
            if got != MENU_DONE and any(w.auth for w in ways):  # a way in: its lines on a screen of their own
                if self.lobby:
                    self.apart.take()
                    click.echo(self.printer.bold(title), err=True)
                else:  # asked on the main screen, under its title
                    self.apart.leave()
                    click.echo(self.printer.bold(title), err=True)
            return None if got == MENU_DONE else got
        self.head(title)
        self.explain(lines)
        while True:  # a plain choice: a word or its letter, anything else asks again, input that ends leaves
            try:
                said = _typed(plain_ways(ways), err=True)
            except EOFError:
                return None
            if (got := way_of(ways, said)) is not None:
                return got

    def close(self) -> None:
        if not self.lobby:
            self.apart.leave()


def _connect_one(root: Path | None, source: str, given: str | None, replace: bool, printer: Any = None,
                 apart: Any = None, title: str = "") -> str:
    """Connect one source and print what was kept, as `connect SOURCE` does. A kind with a login and no file
    of its name yet is connected first and its file written once the connect succeeds, with the
    way in it took. A file that names no way in gets the one the connect took. At a terminal
    the source says what to make and where before it asks, drawn by `printer`, or plain with none. Gives the line
    for the file it added or changed, or ""."""
    import webbrowser

    from .connect import (
        ConnectError,
        NotATerminal,
        PromptBack,
        Tell,
        _with_auth,
        add_source,
        connect,
        logs_in,
        new_source,
        set_auth,
    )
    from .guided import held_terminal

    def ask(label: str, hidden: bool) -> str:  # on stderr, with what the source said
        if printer is not None and printer.styled_out:  # the view's prompt: its legend under it, Escape goes back
            from .checklist import FieldBack, NoRawKeys, field

            try:
                return field(_field_ask(label, hidden), printer)
            except FieldBack:
                raise PromptBack(f"connect {shown(source, 120)}: nothing was kept") from None
            except NoRawKeys:
                pass
        try:
            if not hidden:
                return _typed(label, hidden, err=True, suffix=": ")
            with held_terminal():  # the modes come back on a signal, and Ctrl-\ and Ctrl-Z are keys, as in a key's view
                line = _typed(label, hidden, err=True, suffix=": ")
            return line.translate(dict.fromkeys((0x1C, 0x1A, 0x19)))  # those keys stop nothing, and are no text
        except EOFError:  # at a hidden question it cancels as Ctrl-C does, and elsewhere it answers nothing
            if hidden:
                raise KeyboardInterrupt from None
            return ""

    label = f"sources/{shown(source, 120)}.yaml"
    written: list[str] = []
    with open_workspace(root) as workspace:
        folder = workspace.root
        path = folder / "sources" / f"{source}.yaml"
        adding = not path.exists() and logs_in(source)

        def write(auth: str) -> Any:  # before the login is kept: a file that cannot be written keeps nothing
            try:
                if adding:
                    if path.exists() or path.is_symlink():
                        raise ConnectError(f"{label} came to be while you connected, so nothing was kept: run "
                                           f"cerebro connect {shown(source, 120)} again")
                    text = f"kind: {source}\n" + (f"auth: {auth}\n" if auth else "")
                    undo = functools.partial(_unwrite, path, None, label, text)
                    said = f"added {label}" + (f" with auth: {auth}" if auth else "")
                elif auth:
                    before = path.read_text(encoding="utf-8", newline="")
                    undo = functools.partial(_unwrite, path, before, label, _with_auth(before, auth))
                    said = f"{label} says auth: {auth} now."
                else:
                    return None
                try:  # the undo is known before the write, so Ctrl-C as it lands still takes it back
                    add_source(folder, source, auth) if adding else set_auth(path, auth)
                except BaseException:
                    try:
                        undo()
                    except ConnectError:
                        pass  # the write did not land, or someone wrote the file meanwhile: it stays
                    raise
                written.append(said)
                return undo
            except OSError as error:
                raise ConnectError(f"{label} could not be written ({type(error).__name__}), so nothing was kept: "
                                   f"let it be written, then run cerebro connect {shown(source, 120)} again") \
                    from None

        if not title:  # `connect <source>` alone: its view is headed by its kind's title, as the lobby's are
            from .connect import title_of
            from .source import SourceError, read_source

            try:
                title = title_of(read_source(path).kind) if path.exists() else title_of(source)
            except SourceError:
                title = ""
        tell: Tell | None = _Tell(printer, apart, title) if _asking() and given is None else None
        def step(label: str) -> Any:  # a check's step, printed plain where nothing draws
            from .output import Printer

            return (printer or Printer(plain=True)).step(label)

        try:
            done = connect(workspace, source, given=given, ask=ask if _asking() else None, replace=replace,
                           open_browser=webbrowser.open, say=lambda line: _echo(line, err=True),
                           source=new_source(source) if adding else None,
                           tell=tell, write=write, step=step)
        except NotATerminal as error:
            raise _WaitsToRun(shown(" ".join(str(error).split())), error.run) from None
        finally:
            if isinstance(tell, _Tell):
                tell.close()
    _echo(done.said)
    for line in done.profile:  # who the login is, in profile.md
        _echo(line, err=line.startswith("warning:"))
    if done.note:
        _echo(f"note     {done.note}")
    return written[0] if written else ""


def _field_ask(label: str, hidden: bool) -> Any:
    """A connect question in a view: a default the label offers, as `[<value>, from profile.md]`, is the
    answer an empty Enter takes, drawn as the view's hint."""
    from .checklist import FieldAsk

    found = re.fullmatch(r"(.*) \[(.+?)(?:, (from .+))?\]", label)
    if found:
        return FieldAsk(found.group(1), default=found.group(2), source=found.group(3) or "", hidden=hidden)
    return FieldAsk(label, hidden=hidden)


def _cli_owner(root: Path | None, name: str) -> Any:
    """The connector of the source `name` when another program holds its login, as a command-line tool of the
    provider's holds it, or None for any other: the source's own file says its kind, else a kind of that name not
    added yet."""
    from .connectors import CONNECTORS
    from .source import SourceError, read_source

    path = (root if root is not None else Path.cwd()).resolve() / "sources" / f"{name}.yaml"
    try:
        kind = read_source(path).kind if path.exists() else name
    except SourceError:
        return None
    built = CONNECTORS.get(kind)
    return built if getattr(built, "cli_login", None) is not None else None


def _connect_by_cli(root: Path | None, name: str, printer: Any, said: Any, given: str | None = None,
                    apart: Any = None) -> list[str]:
    """Connect a source whose login another program holds: what it needs and
    the line that logs it in, on a tint, then `Confirm ... ? [Y/n]` at a view's prompt, where n or Escape goes back.
    Plain, with no `printer`, there is no view and no question. Then the program is looked
    for on the PATH, the connector picks what the source connects, from `given`, the value of its own flag, from
    the file, or from the program, asking a person where one answers, and the login is read once, with a step, as a
    pass reads it; nothing is kept, since the program holds the login. A kind not added yet gets the file its
    connector writes. `apart` is the lobby's screen apart, which a question taller than it leaves. Gives the closing
    lines. Raises ConnectBack for no, and ConnectError when the program or its login is missing."""
    import shutil

    from .checklist import FieldAsk, FieldBack, field
    from .connect import ConnectBack, ConnectError, new_source, write_source
    from .connect_screen import explained
    from .connectors.connecting import KeyRefused, KeyUnproven
    from .output import Printer
    from .source import read_source

    title = getattr(said, "title", "") or name
    view = said.cli_login
    if printer is not None:  # plain, there is no view: the check runs at once
        for line in explained(printer, [view.says]):
            click.echo(line, err=True)
        click.echo("", err=True)
        for line in printer.tinted(view.block):
            click.echo(line, err=True)
        click.echo("", err=True)
        try:
            if field(FieldAsk(view.confirm, yes_no=True, default_yes=True), printer) != "y":
                raise ConnectBack(f"connect {name}: nothing was kept")
        except FieldBack:
            raise ConnectBack(f"connect {name}: nothing was kept") from None
    if shutil.which(view.program) is None:
        raise ConnectError(f"{title}: {view.missing}")
    folder = (root if root is not None else Path.cwd()).resolve()
    path = folder / "sources" / f"{name}.yaml"
    source = read_source(path) if path.exists() else new_source(name)
    said_by = printer or Printer(plain=True)
    with open_workspace(root) as workspace:
        try:
            source = said.pick_cli(source, workspace, given, _line_ask(printer, name, apart), said_by.step)
        except (KeyRefused, KeyUnproven) as error:
            raise ConnectError(shown(" ".join(str(error).split()), 300)) from None
        step = said_by.step(f"Verifying {title} connection")
        try:
            closing, text = said.check_cli(source, workspace)
        except (KeyRefused, KeyUnproven) as error:
            step.fail(f"{title} did not verify: {error.why}")
            raise ConnectError(shown(" ".join(str(error).split()), 300)) from None
    step.done(f"{title} connection verified")
    lines = [closing]
    if not path.exists():
        write_source(path, None, text)
        lines.append(f"Added sources/{name}.yaml.")
    return lines


def _drop_typed() -> None:
    """Drop what was typed at the terminal and not yet read, before a prompt: keys pressed while a step ran are no
    answer."""
    try:
        import termios

        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):
        pass


def _line_ask(printer: Any, name: str, apart: Any = None) -> Callable[[list[str], str, str], str | None] | None:
    """How a connector asks a person one line: it shows `notes`, then asks `prompt`
    with `hint` in brackets, and gives the line typed, or None at the end of the input. Where a view draws, the
    prompt is the view's, with its legend, and Escape goes back with nothing kept; on a plain terminal it is a plain
    line. A question taller than the lobby's screen apart, which keeps no scrollback, is asked on the main screen.
    Off a terminal there is no one to ask, so None."""
    if not _asking():
        return None
    import shutil

    from .checklist import FieldAsk, FieldBack, NoRawKeys, field
    from .connect import ConnectBack
    from .output import visible_width

    def ask(notes: list[str], prompt: str, hint: str) -> str | None:
        _drop_typed()  # what was typed while a step ran is not an answer, and is never drawn
        if apart is not None and apart.on:
            cols = max(shutil.get_terminal_size().columns, 1)
            if not apart.fits(sum(max(1, -(-visible_width(note) // cols)) for note in notes) + 3):
                apart.leave()
        for note in notes:
            click.echo(note, err=True)
        if printer is not None:
            try:
                return field(FieldAsk(prompt, hint=hint), printer)
            except FieldBack:
                raise ConnectBack(f"connect {name}: nothing was kept") from None
            except NoRawKeys:
                pass
        click.echo(f"{prompt} [{hint}] ", nl=False, err=True)
        line = sys.stdin.readline()
        if not line:
            click.echo("", err=True)
            return None
        return line

    return ask


def _unwrite(path: Path, before: str | None, label: str, written: str | None = None) -> None:
    """Take back a source file connect wrote, for a login that could not be kept: delete the file it added, or put
    back the text it had before, byte for byte, over the text `written` it wrote, and never over an edit made
    since. ConnectError says what is left to fix by hand."""
    from .connect import ConnectError, write_source

    try:
        if before is None:
            if _text(path) != written:
                raise ConnectError("changed")
            path.unlink()
        else:
            write_source(path, written, before)
    except (OSError, ConnectError) as error:
        why = type(error).__name__ if isinstance(error, OSError) else "it changed meanwhile"
        fix = "delete it" if before is None else "take out the auth: line connect added as its last line"
        raise ConnectError(f"{label} could not be put back as it was ({why}): {fix}") from None


def _text(path: Path) -> str | None:
    """A file's text as written, its line endings kept, or None when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8", newline="")
    except (OSError, UnicodeDecodeError):
        return None


def _pick(root: Path | None, name: str, printer: Any) -> str:
    """Show what the source's connector offers for its file as a list to tick, write the ticks into the file on
    Enter, and give the line that says what the file lists now. The file is read before anything else, and checked
    again once the list is read and at the write, so an edit made meanwhile is never written over. A connector that
    offers nothing leaves the file to the person, and the line says so. `printer` draws the list, or, plain, prints
    it and asks on a plain line."""
    from .connect import ConnectError, answers, write_source
    from .connect_screen import pick, written_line
    from .connector import ListKept
    from .source import read_source

    label = f"sources/{shown(name, 120)}.yaml"
    path = (root if root is not None else Path.cwd()).resolve() / "sources" / f"{name}.yaml"
    before = _text(path)
    with open_workspace(root) as workspace:
        connector = workspace.connectors.get(name)
        if connector is None:  # the file reads, but the workspace refused its connector: say why
            why = next((line for line in workspace.refused if line.startswith((f"sources/{name}.yaml ",
                                                                           f"sources/{name}.yaml:"))), "")
            return shown(" ".join(why.split()), 300) or f"{label} is not a source the workspace reads"
        if before is None or not answers(read_source(path).kind):
            return ""  # no list to pick: the pass scopes what it reads
        offer = _offer(workspace, connector, name, printer)
        if offer is None:
            return f"{label} is written by hand: edit it to choose what it reads, then run cerebro serve --once"
        if _text(path) != before:  # changed, or gone
            raise ConnectError(f"{label} changed while its list was read, so nothing was shown or written: run "
                               f"cerebro connect {shown(name, 120)} again")
        values = pick(offer, name, printer, answer=_answer)
        if values is not None:
            try:
                text = connector.written(before, offer, values)
            except ListKept as error:
                raise ConnectError(f"{label} is left as it is, since {error}: list them under {offer.key}: by "
                                   "hand") from None
            write_source(path, before, text)
        return written_line(offer, name, values)


def _offer(workspace: Cerebro, connector: Any, name: str, printer: Any) -> Any:
    """What the connector offers, with a step while it is read and grouped, or None for one that offers nothing."""
    step = printer.step(f"Reading what sources/{shown(name, 120)}.yaml can list")
    try:
        offer = connector.offer(workspace.asking())
    except BaseException:
        step.clear()
        raise
    if offer is None:
        step.clear()
        return None
    n = offer.listed or len(offer.choices)
    step.done(f"Listed {n} {offer.one if n == 1 else offer.many}")
    return offer


def _open(root: Path | None, place: Any, printer: Any, apart: Any = None) -> list[str]:
    """Open one row of the lobby, or one name of the plain line: connect its source and show its list, and give
    the lines that say how it went. A kind not added yet is connected first, and its file written only once the
    connect succeeds. In the lobby, on its screen `apart`, the view draws from the top
    of a clear screen under the row's title, and so does its list. Raises what the
    connect raises."""
    lobby = apart is not None and apart.on
    title = getattr(place, "title", "") or place.name
    if lobby:
        apart.take()
        click.echo(printer.bold(title), err=True)
    added = _connect_one(root, place.name, None, False, printer, apart, title)
    if lobby:  # back apart, as a view too tall for it left it
        apart.take()
    picked = _pick(root, place.name, printer)
    return [f"{shown(getattr(place, 'title', '') or place.name, 120)} connected", *([added] if added else []),
            *([picked] if picked else [])]


def _fails(error: BaseException) -> str:
    why = error.format_message() if isinstance(error, click.ClickException) else str(error)
    return " ".join(why.split())


def _no_connect(root: Path | None, place: Any) -> str:
    """The line for a row whose source has no login to connect, or "" for one connect can run: a source that keeps
    no login, or a kind with none, whose file a person writes."""
    from .connect import ConnectError, logs_in, need
    from .connect_screen import NO_LOGIN, NOT_ADDED
    from .source import SourceError, read_source

    name = shown(place.name, 120)
    if place.state == NOT_ADDED and not logs_in(place.name):
        return f"{name}: write sources/{name}.yaml by hand, with kind: {name} and what it reads"
    if place.state != NO_LOGIN:
        return ""
    try:
        what = need(read_source((root if root is not None else Path.cwd()).resolve() / "sources" /
                                f"{place.name}.yaml")).what
    except (SourceError, ConnectError, OSError) as error:
        what = str(error)
    return f"{name}: {shown(' '.join(what.split()), 300)}"


def _failed(name: str, why: str, printer: Any, title: str = "") -> str:
    """Say a connect failed, on stderr, and the one command to run, the one the failure names, else connect again;
    give the status line."""
    from .output import Mark, command_in

    line = f"{printer.mark(Mark.FAILED)} {shown(title or name, 120)}: {shown(why, 300)}"
    click.echo(line, err=True)
    click.echo(f"  Run: {shown(command_in(why) or f'cerebro connect {name}', 300)}", err=True)
    return line


def _end(root: Path | None, failed: dict[str, str], printer: Any) -> None:
    """The last line: each row that needs attention, or what to connect next, the first
    source not connected, else the first kind not added that connect can add; once there is none, a pass."""
    from .connect import logs_in
    from .connect_screen import ATTENTION, NOT_ADDED, NOT_CONNECTED
    from .output import command_in

    ends = _rows(root, failed)
    for p in ends:
        if p.state == ATTENTION:
            fix = command_in(" ".join(p.reason.split())) or f"cerebro connect {p.name}"
            printer.check(f"{shown(p.name, 120)} needs attention: run {shown(fix, 300)}")
    waiting = next((p for p in ends if p.state == NOT_CONNECTED), None) \
        or next((p for p in ends if p.state == NOT_ADDED and logs_in(p.name)), None)
    printer.next(f"cerebro connect {shown(waiting.name, 120)}" if waiting else "cerebro serve --once")
    click.echo(printer.last_line(), err=True)


def _lobby(root: Path | None, printer: Any, end: bool = True) -> None:
    """The lobby: the workspace's sources and the kinds it could add, until Escape or Done. Each row opened runs
    that source's connect and its picker; a kind not added is added once its connect succeeds. An empty first
    answer connects it later, with no failure. A connect that fails says so with the
    command to run, and its row needs attention, with why, until a connect succeeds. A workspace that does not open
    says why and ends the command. On the way out, the last line names each row
    that needs attention, or what to connect next."""
    from .connect_screen import Apart

    status: list[str] = []
    failed: dict[str, str] = {}
    at, opened = 0, ""
    apart = Apart(printer)  # the lobby and the views it opens draw on the alternate screen
    try:
        _lobby_rows(root, printer, apart, status, failed, at, opened)
    finally:
        apart.leave()
    if end:  # init's walk goes on to its next step instead
        _end(root, failed, printer)


def _lobby_rows(root: Path | None, printer: Any, apart: Any, status: list[str], failed: dict[str, str], at: int,
                opened: str) -> None:
    """The lobby's menu on its screen apart, until Escape or Done, each row opened in turn."""
    from .connect import ConnectBack, ConnectLater
    from .connect_screen import lobby
    from .output import Mark

    while True:
        rows = [p for p in _rows(root, failed) if p.viewed]  # the lobby shows only what connect has a view for
        at = next((i for i, p in enumerate(rows) if p.name == opened), at)  # the cursor stays on the row opened
        apart.take()
        got = lobby(rows, printer, at, status)
        if got is None:
            break
        at, place = got, rows[got]
        opened = place.name
        if (said := _cli_owner(root, place.name)) is not None:  # a login another program holds: its view
            apart.take()
            click.echo(printer.bold(place.title or place.name), err=True)
            try:
                lines = _connect_by_cli(root, place.name, printer, said, apart=apart)
            except ConnectBack:
                status = []
                continue
            except (CerebroError, click.ClickException) as error:
                failed[place.name] = _fails(error)
                status = [_failed(place.name, failed[place.name], printer, place.title)]
                continue
            failed.pop(place.name, None)
            status = [f"{printer.mark(Mark.DONE)} {place.title or place.name} connected", *lines[1:]]
            continue
        if line := _no_connect(root, place):
            status = [line]
            continue
        try:
            said = _open(root, place, printer, apart)
        except (ConnectLater, ConnectBack):
            failed.pop(place.name, None)
            status = []
            continue
        except (CerebroError, click.ClickException) as error:
            failed[place.name] = _fails(error)
            status = [_failed(place.name, failed[place.name], printer, place.title)]
            continue
        failed.pop(place.name, None)
        status = [f"{printer.mark(Mark.DONE)} {said[0]}", *said[1:]]


def _walk(root: Path | None, rows: list[Any]) -> bool:
    """At a terminal that draws nothing, ask which sources to connect,
    by name, with commas, and connect each in turn, its list printed plain to accept or quit, then the last line,
    as the lobby's. Whether any was named."""
    from .connect import ConnectBack, ConnectLater
    from .output import Printer, command_in

    names = {p.name.casefold(): p for p in rows}
    plain = Printer(plain=True)
    while True:
        try:
            said = _answer("Connect which? Names, with commas [Enter to skip]: ")
        except EOFError:
            said = ""
        picked = [part.strip() for part in said.split(",") if part.strip()]
        unknown = [part for part in picked if part.casefold() not in names]
        if not unknown:
            break
        _echo(f"{unknown[0]} is not one of {', '.join(p.name for p in rows)}", err=True)
    if not picked:
        _echo("Nothing picked, nothing connected.")
        _end(root, {}, plain)
        return False
    for part in picked:
        place = names[part.casefold()]
        _echo("")
        _echo(place.name)
        if line := _no_connect(root, place):
            _echo(line)
            continue
        while True:  # a connect that fails asks to retry or to connect it later
            try:
                for line in _open(root, place, plain)[1:]:
                    _echo(line)
            except ConnectLater as later:
                _echo(str(later))
                if later.check:
                    plain.check(later.check)
            except ConnectBack:
                pass
            except (CerebroError, click.ClickException) as error:
                why = _fails(error)
                _failed(place.name, why, plain)
                if _retry(place.name):
                    continue
                plain.check(f"{shown(place.name, 120)} is not connected yet: run "
                            f"{shown(command_in(why) or f'cerebro connect {place.name}', 300)}")
            break
    _end(root, {}, plain)
    return True


def _retry(name: str) -> bool:
    """Ask whether to retry a connect that failed or connect it later; Enter, or input that
    ends, connects it later."""
    while True:
        try:
            said = _typed(f"{shown(name, 120)} did not connect.  [r]etry  [c]onnect later  > ", err=True)
        except EOFError:
            return False
        said = said.strip().lower()
        if said in ("r", "retry", "", "c", "connect later"):
            return said in ("r", "retry")


def _start(root: Path | None, name: str, plain: bool) -> bool:
    """`connect <name>` for the source a connector adds by that name: use the
    source of that name, else the first other source of its kind, else add its file; make the
    folder it reads, and say how many records it reads now. Whether `name` is such a source; one whose file is of
    another kind, with no other source of its kind, is not. `plain` prints the last line plain."""
    from .connect import ConnectError, count, starter, write_source
    from .source import SourceError, read_source, source_of

    begun = starter(name)
    if begun is None:
        return False
    from .output import Printer

    kind = source_of(name, begun.text).kind
    with open_workspace(root) as workspace:
        folder = workspace.root / "sources"

        def of_kind(path: Path) -> bool:
            try:
                return read_source(path).kind == kind
            except SourceError:
                return False

        own = folder / f"{name}.yaml"
        found = own if own.exists() and of_kind(own) else next(
            (p for p in sorted(folder.glob("*.yaml")) if p != own and of_kind(p)), None)
        if found is None and own.exists():
            return False
        reads = workspace.root / begun.folder
        if reads.exists() and not reads.is_dir():
            raise click.ClickException(f"{begun.folder} is a file, not a folder, so nothing was written: move it "
                                       f"aside, then run cerebro connect {name} again")
        said = []
        if found is None:
            write_source(own, None, begun.text)
            said.append(begun.added.replace("{name}", name))
            found = own
        reads.mkdir(exist_ok=True)
    used = found.name.removesuffix(".yaml")
    for line in said:
        _echo(line)
    printer = Printer(plain=plain)
    step = printer.step(f"Counting the {begun.noun}s in {begun.folder}/")  # a step
    with open_workspace(root) as workspace:
        try:
            n = count(workspace, used)
        except ConnectError as error:
            step.fail(f"sources/{shown(used, 120)}.yaml did not open")
            raise click.ClickException(shown(" ".join(str(error).split()), 300)) from None
    step.done(begun.counted.replace("{name}", shown(used, 120)).replace("{count}", _count(n, begun.noun)))
    printer.next("cerebro serve --once")
    click.echo(printer.last_line(), err=True)
    return True


def _pick_off_terminal(root: Path | None, name: str) -> None:
    """After a connect off a terminal, the lines on how to pick: for a kind that picks a list, where to
    pick it, and, while the file lists nothing, a line to check."""
    from .connect import picks
    from .source import SourceError, read_source

    path = (root if root is not None else Path.cwd()).resolve() / "sources" / f"{name}.yaml"
    try:
        source = read_source(path)
    except SourceError:
        return
    if not (listed := picks(source.kind)):
        return
    key, one, many = listed
    where = f"run cerebro connect {name} in a terminal, or list them under {key}: in sources/{name}.yaml"
    _echo(f"Pick the {many} to read: {where}.")
    if not source.settings.get(key):
        _echo(f"check     no {one} is picked yet: {where}")


class _ConnectUsage(click.UsageError):
    """A usage error of `connect`: its line, then the failed line, exit 2. The line never quotes a word
    typed on connect's line, which may be a login pasted in the wrong place."""

    def __init__(self, message: str, run: str = "cerebro connect --help", code: int = 2):
        super().__init__(message)
        self.run = run
        self.exit_code = code

    def show(self, file: Any = None) -> None:
        click.echo(f"Error: {self.format_message()}", err=True)
        click.echo(_failed_line(self.run), err=True)


class _ConnectCommand(_Leaf):
    """`connect`, whose usage errors say what it takes and print back no word it was given."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.NoSuchOption:
            raise _ConnectUsage(f"connect has no such flag: it takes --stdin, --from-env NAME, --replace, --plain, "
                                f"{', '.join(_connect_flags() + [''])}and --workspace") from None
        except click.UsageError as error:
            if "unexpected extra argument" in error.format_message():
                raise _ConnectUsage("connect takes one source's name, and was given more: a login goes on stdin "
                                    "with --stdin, or in a variable named with --from-env NAME, never on the "
                                    "command line") from None
            if isinstance(error, click.MissingParameter) or "requires an argument" in error.format_message():
                raise _ConnectUsage(error.format_message()) from None  # it names the flag or the argument alone
            raise _ConnectUsage("connect could not read what it was given") from None


def _need_of(root: Path | None, name: str) -> Any:
    """What the source `name` of the workspace at `root` needs, read from its file, or from its kind for one connect
    would add, or None when that cannot be told."""
    from .connect import ConnectError, logs_in, need, new_source
    from .source import SourceError, read_source

    path = (root if root is not None else Path.cwd()).resolve() / "sources" / f"{name}.yaml"
    try:
        return need(read_source(path)) if path.exists() else need(new_source(name)) if logs_in(name) else None
    except (SourceError, ConnectError, OSError):
        return None


def _known(root: Path | None, name: str) -> list[str] | None:
    """None when `name` is a source of the workspace, a kind of source or a source a connector starts; else the
    names connect takes, to say in its place."""
    from .connect import logs_in, starter

    rows = _places(root)
    if not rows or any(p.name == name for p in rows) or logs_in(name) or starter(name) is not None:
        return None  # a workspace that does not open says why where it is opened
    return [p.name for p in rows]


_FLAG = "cli_flag_"  # the parameter name of a flag a connector adds to connect, before its kind


def _cli_flags(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The flags connectors whose login another program holds add to `connect`, each for its own kind alone."""
    from .connectors import CONNECTORS

    for kind, built in sorted(CONNECTORS.items(), reverse=True):
        view = getattr(built, "cli_login", None)
        if view is not None and view.flag:
            fn = click.option(f"--{view.flag}", f"{_FLAG}{kind}", default=None, metavar="NAME",
                              help=view.flag_help)(fn)
    return fn


def _connect_flags() -> list[str]:
    """Each flag a connector adds to `connect`, as `--name NAME`."""
    from .connectors import CONNECTORS

    return [f"--{view.flag} NAME" for built in CONNECTORS.values()
            if (view := getattr(built, "cli_login", None)) is not None and view.flag]


@main.command("connect", cls=_ConnectCommand)
@click.argument("source", cls=_SourceOrLobby)
@_root_option
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the login from standard input.")
@click.option("--from-env", "from_env", default=None, metavar="NAME",
              help="Read the login from the environment variable NAME.")
@click.option("--replace", "replace_kept", is_flag=True, help="Ask for a new login even when the kept one works.")
@click.option("--plain", is_flag=True, help="Print plain lines: no lobby or list is drawn.")
@_cli_flags
@_refusing
def connect_command(source: str | None, root: Path | None, from_stdin: bool, from_env: str | None,
                    replace_kept: bool, plain: bool, **flags: str | None) -> None:
    """Connect SOURCE, the file sources/SOURCE.yaml: get the login its kind needs, check it with one read-only
    call, and keep it in the workspace's secrets store. A browser sign-in opens the system browser. Off a terminal it
    asks nothing: give the login with --stdin or --from-env, or it exits 2. At a terminal, a source whose kind can
    list what its file may name then shows it to tick, and Enter writes the ticked ones into its file. With no
    SOURCE, at a terminal, it lists the workspace's sources and the kinds it could add, to pick one. Ctrl-C ends it with exit 130, nothing printed and nothing kept."""
    try:
        _connect(source, root, from_stdin, from_env, replace_kept, plain,
                 {k.removeprefix(_FLAG): v for k, v in flags.items() if v is not None})
    except KeyboardInterrupt:
        raise click.exceptions.Exit(INTERRUPTED) from None
    except OSError as error:  # a folder the workspace or its secrets store needs, which cannot be set up
        where = f" at {shown(str(error.filename), None)}" if error.filename else ""
        if click.get_current_context().meta.get("connect.kept"):
            raise click.ClickException(f"the login is kept, but connect could not read what follows "
                                       f"({type(error).__name__}{where})") from None
        raise click.ClickException(f"connect could not set up what it needs ({type(error).__name__}{where}), so "
                                   f"nothing was kept: make that folder writable, then connect again") from None


def _connect(source: str | None, root: Path | None, from_stdin: bool, from_env: str | None, replace_kept: bool,
             plain: bool, flags: dict[str, str] | None = None) -> None:
    flags = flags or {}
    if root is not None and not (Path(root) / "settings.yaml").is_file():  # its name is not printed back
        raise click.ClickException("the folder given with --workspace is not a workspace (no settings.yaml in it): "
                                   "name the folder that holds settings.yaml")
    printer = _drawing(plain)
    if _terminal_out():  # the header, at a terminal: the command, then the workspace it acts on
        try:
            with open_workspace(root) as workspace:
                folder = workspace.root
        except (CerebroError, click.ClickException, OSError):
            folder = None  # the command says why where it fails
        if folder is not None:
            from .output import Printer

            (printer or Printer(plain=True, terminal=lambda stream: True)).header("cerebro connect",
                                                                                  shown(str(folder), None))
    if source is None:
        if printer is not None:
            _lobby(root, printer)
            return
        ctx = click.get_current_context()
        param = next(p for p in ctx.command.params if p.name == "source")
        rows = _places(root)
        if not _asking() or not rows:
            raise _NoSource(ctx, param, _table(rows, root), [p.name for p in rows], plain)
        for line in _table(rows, root):  # a person at a terminal that draws nothing names them instead
            _echo(line)
        _walk(root, rows)  # naming nothing connects nothing, and exits 0
        return
    if (names := _known(root, source)) is not None:  # never printed back: it may be a login in the wrong place
        raise _ConnectUsage(f"the name given is not one of the workspace's sources or kinds of source, "
                            f"{', '.join(shown(n, 120) for n in names)}: pass one of them, and a login only with "
                            f"--stdin or --from-env NAME", "cerebro connect", 1)
    if flags:  # a connector's own flag is for its kind alone: any other kind refuses it
        from .connectors import CONNECTORS

        owner = _cli_owner(root, source)
        for kind in flags:
            if owner is None or CONNECTORS.get(kind) is not owner:
                raise _ConnectUsage(f"--{CONNECTORS[kind].cli_login.flag} is for connect {kind}",  # type: ignore[attr-defined]
                                    "cerebro connect --help", 1)
    needed = _need_of(root, source)
    what = needed.what if needed is not None else "the login"
    if replace_kept and not from_stdin and from_env is None and not _asking() and needed is not None \
            and needed.way not in ("browser", "none", "cli"):
        from_stdin = True  # off a terminal, --replace reads the login piped to it
    if from_stdin and from_env is not None:
        raise _ConnectUsage(f"--from-env and --stdin both give {what}: pass one")
    given = None
    if from_stdin:
        given = sys.stdin.read()
        if not given.strip():
            raise _ConnectUsage(f"--stdin gave no value on stdin for {what}")
    elif from_env is not None:
        given = os.environ.get(from_env)
        if not given:  # the name is not printed back: a login given in its place would be
            raise _ConnectUsage(f"--from-env names a variable that is empty or not set, so {what} has no value")
    if given is None and not replace_kept and _start(root, source, printer is None):
        return
    if given is None and (owner := _cli_owner(root, source)) is not None:
        from .connect import ConnectBack, ConnectError
        from .output import Printer

        end = printer or Printer(plain=True)
        if printer is not None:  # headed, as every view is
            click.echo(printer.bold(getattr(owner, "title", "") or source), err=True)
        try:
            lines = _connect_by_cli(root, source, printer, owner, next(iter(flags.values()), None))
        except ConnectBack:
            click.echo(end.last_line(), err=True)
            return
        except ConnectError as error:
            raise click.ClickException(str(error)) from None
        for line in lines:
            _echo(line)
        end.next("cerebro serve --once")
        click.echo(end.last_line(), err=True)
        return
    from .connect import ConnectBack, ConnectLater

    try:
        added = _connect_one(root, source, given, replace_kept, printer)
        click.get_current_context().meta["connect.kept"] = True
    except (ConnectLater, ConnectBack) as stop:  # connect it later, or Escape: done, with nothing kept
        from .output import Printer

        end = printer or Printer(plain=True)
        if isinstance(stop, ConnectLater):
            _echo(str(stop))
            if stop.check:
                end.check(stop.check)
        click.echo(end.last_line(), err=True)
        return
    if added:
        _echo(added)
    if from_stdin:
        _pick_off_terminal(root, source)
    elif printer is not None:
        if said := _pick(root, source, printer):
            _echo(said)
        else:  # a source with no list ends: done, and the check to run next
            printer.next(f"cerebro sources check {shown(source, 120)}")
            click.echo(printer.last_line(), err=True)
    elif _asking():
        from .connect import answers
        from .output import Printer
        from .source import SourceError, read_source

        try:
            kind = read_source((root if root is not None else Path.cwd()).resolve() / "sources" /
                               f"{source}.yaml").kind
        except SourceError:
            kind = ""
        if answers(kind):  # a terminal that draws nothing: the list printed plain, to accept or quit
            _echo(_pick(root, source, Printer(plain=True)))
        else:  # a source with no list ends: done, and the check to run next
            plain = Printer(plain=True)
            plain.next(f"cerebro sources check {shown(source, 120)}")
            click.echo(plain.last_line(), err=True)
    else:
        _pick_off_terminal(root, source)


@main.command("eval")
@click.argument("suite", type=click.Path(path_type=Path, dir_okay=False, exists=True))
@_root_option
@click.option("--group", default="", help="Run only the cases of this group.")
@click.option("--workers", type=int, default=4, show_default=True, help="How many cases run at once.")
@click.option("--no-model", "no_model", is_flag=True, help="Judge by rules and words alone, with no model call.")
@click.option("--cap", type=float, default=None,
              help="The evaluation layer's dollars for today, in place of settings.yaml's evaluation_daily_usd.")
@_json_option
@_refusing
def eval_command(suite: Path, root: Path | None, group: str, workers: int, no_model: bool, cap: float | None,
                 as_json: bool) -> None:
    """Run the eval SUITE, a file of questions to ask the wiki or a grounding corpus, and print its scores. The
    decision model judges, its spend kept under the evaluation layer and held to its cap."""
    from . import evals

    loaded = evals.load(suite)
    with open_workspace(root) as workspace:
        decider = None if no_model else workspace.decider
        report = evals.run(workspace, loaded, decider=decider, model=workspace.models["decision"], ledger=workspace.ledger,
                           cap_usd=cap if cap is not None else workspace.ledger.caps.evaluation, clock=workspace.clock,
                           group=group, workers=max(1, workers))
    if as_json:
        _print_json(report)
        return
    for line in evals.lines(report):
        _echo(line)


# -- the first run's list -------------------------------------------------------------------------------------------

def _screen(yes: bool, plain: bool) -> Callable[[Any], Any] | None:
    """The screen a first run's list is shown on: with --yes it is printed and what it ticks approved; at a
    terminal the checkbox list, or
    with --plain the plain list and its accept line; off a terminal none, and the list waits. It sees the list and
    gives the choices, and the engine applies them: it reads nothing of the workspace."""
    from .first_run import Choices, lines

    if not (yes or plain or _asking()):
        return None

    def choose(pile: Any) -> Any:
        if yes:
            choices = Choices.starter(pile)
            for line in lines(pile):
                _echo(line)
            if choices.accept and len(choices.leave) > sum(1 for row in pile.rows() if row.left_out):
                _echo(f"approved  {len(choices.accept)} ticked, and none left out, with --yes: the rest wait for "
                      "cerebro review")
            elif choices.accept:
                _echo("approved  every proposed article, and none left out, with --yes")
            else:
                _echo("approved  nothing with --yes: nothing was ticked: the rest wait for cerebro review")
            return choices
        if not plain and _asking():
            return _checklist(pile)
        for line in lines(pile):
            _echo(line)
        return _accept_line(pile)

    return choose


def _checklist(pile: Any) -> Any:
    """The checkbox list, at a terminal that takes raw keys: the choices once Enter is pressed, or None for Escape.
    Where the terminal refuses raw keys, or NO_COLOR asks for plain text, or the list holds no row, the plain list
    and its accept line stand in. The widget and Textual load here, only when a list is drawn."""
    from .first_run import lines

    if pile.rows():
        from .checklist import NoRawKeys
        from .first_run_list import draw

        try:
            return draw(pile)
        except NoRawKeys:
            pass
    for line in lines(pile):
        _echo(line)
    return _accept_line(pile)


def _accept_line(pile: Any) -> Any:
    """The plain list's one line: a note that names the hand admit, then `[a]ccept  [q]uit  > ` on standard error,
    asked until it is answered a or q. The end of the input quits, and Ctrl-C ends the command with 130, as at every
    line asked."""
    from .first_run import Choices, proposers

    # the hand admit with the collection of the first by id of the curators that proposed the list's rows, or of any
    # curator when none did
    of = {listed.agent: listed.collection for listed in pile.lists}
    first = next((of[id] for id in proposers(pile) or sorted(of) if of.get(id)), "")
    form = f"cerebro admit {shown(first, 80) if first else '<collection>'}/<name>"
    click.echo("", err=True)
    click.echo(f"Accept admits the ticked ones, and the rest wait. Add with {form}.", err=True)
    while True:
        try:
            said = str(click.prompt("[a]ccept  [q]uit  > ", default="", show_default=False, prompt_suffix="",
                                    err=True))
        except click.Abort as error:
            if isinstance(error.__context__, KeyboardInterrupt):
                raise click.exceptions.Exit(130) from None
            click.echo("", err=True)
            return None
        answer = said.strip().lower()
        if answer in ("a", "accept"):  # what the plain list ticks, as Enter and --yes take them
            return Choices.starter(pile)
        if answer in ("q", "quit"):
            return None


def _see_first(under_init: bool) -> str:
    """At a terminal, before a first run's discovery: what the person wants to see first, or "" for Enter. Under
    init, a blank line parts it from the Ready block."""
    try:
        if under_init:
            click.echo("", err=True)
        return str(click.prompt("What do you want to see first?", default="", show_default=False, err=True)).strip()
    except click.Abort:
        return ""


def _ask_more(spent: float, more: float) -> bool:
    """At a terminal, when discovery reached its cap: continue for about `more`, or review what it found."""
    for _ in range(3):
        try:
            said = str(click.prompt(f"Discovery has spent ${spent:,.2f} and is not done. Continue for about "
                                    f"${more:,.2f} more, or review what it found so far? [c]ontinue, [r]eview",
                                    default="", show_default=False)).strip().lower()
        except click.Abort:
            return False
        if said in ("c", "continue"):
            return True
        if said in ("r", "review"):
            return False
    return False


def _first_failures(say: Callable[[str], None]) -> tuple[Callable[[str], None], dict[str, str]]:
    """A `say` for a first run that keeps each `failed` line it is handed, by what failed, the last why standing, in
    place of printing it, so the pass's count holds what still fails and it prints once among the pass's failures."""
    kept: dict[str, str] = {}

    def told(line: str) -> None:
        what, _, why = line.removeprefix("failed    ").partition(": ")
        if line.startswith("failed    ") and why:
            kept[what] = why
        else:
            say(line)
    return told, kept


def _pass_fails(result: PassResult, agents: Any = (), admitted: int = 0) -> bool:
    """Whether a pass exits 1: a source that could not
    be read fails it, whatever else landed; any other failure fails it only when nothing landed, which is no article
    written, no run of `agents` that ran, a curator's included, no fact stored or retracted, and none of `admitted`,
    the articles a first run's approval admitted."""
    roles = {agent.id: agent.role for agent in agents}
    if any(key.startswith("sources/") and key.count("/") == 1 and key not in roles for key in result.failed):
        return True
    ran = any(roles.get(run.agent) == "curator" and run.status in ("done", WROTE_NOTHING, "budget spent")
              for run in result.runs)
    landed = result.compiled or result.ran or ran or result.extracted or result.retracted or admitted
    return bool(result.failed) and not landed


def _print_first_run(out: Any, workspace: Cerebro | None = None, failures: dict[str, str] | None = None,
                     screen: Any = None) -> None:
    """What a first run did: each refusal of a decision, then the pass that built what was approved with the late
    lists' lines among its warnings, or the line that says the list waits, which exits 2. `failures` are what failed
    in its rounds: one that still fails, as a curator whose last run failed or what the pass failed again, counts
    with the pass's own, and one that ran since without failing is a warning. It exits 1 as `_pass_fails` says, the
    articles its approval admitted counting as work that landed. On a terminal's `screen`, the run ends as
    `_print_end` ends it, with the pass that built."""
    root = workspace.root if workspace is not None else None
    if out.result is not None and workspace is not None:
        for what, why in (failures or {}).items():
            if what in out.result.failed or failing(workspace, what):
                out.result.failed.setdefault(what, why)
            else:
                out.result.warnings.append(f"{what}: failed in this first run, and did not fail when it ran after: "
                                           f"{why}")
    elif out.result is None:
        for what, why in (failures or {}).items():
            _echo(f"failed    {what}: {why}")
    already = [line for applied in out.applied for line in applied.already.values()]  # decided the other way meanwhile
    for applied in out.applied:
        masked = _masking(list(applied.refused.values()), root)
        for id, why in applied.refused.items():
            if screen is not None and out.result is not None:  # said once the region is drawn a last time
                out.result.refused.append(f"{id}: {masked(why)}")
            else:
                _echo(f"refused   {id}: {masked(why)}")
    if out.quit:
        for line in out.lines:
            _echo(line)
        return
    if out.result is None:
        if out.failed:  # every curator that could propose failed, each said on its own line as it landed: the
            # last line names the failure's own command, or the run again, and the run fails
            from .output import Printer

            click.echo(Printer().last_line(failed=True, run=out.run or "cerebro serve --once"), err=True)
            raise SystemExit(1)
        if out.waits and out.pile is not None and not out.applied:  # off a terminal: the list, then how to decide it
            from .first_run import lines

            for line in lines(out.pile):
                _echo(line)
        for line in out.lines:  # a list that waits says so; one that ends with nothing built is said plain
            _echo(f"waiting   {line}" if out.waits else line)
        for line in [*already, *getattr(out, "after", ())]:  # how many wait, when nothing was approved
            _echo(line)
        if out.waits:
            raise SystemExit(2)
        if out.next:  # what to run next
            _echo(f"next      {out.next}")
        return
    # how many rows of the list wait: on a terminal it is to check, which the last line names
    after = list(getattr(out, "after", ()))
    out.result.warnings += [*out.lines, *(after if screen is not None else [])]
    agents = workspace.agents if workspace is not None else ()
    fails = _pass_fails(out.result, agents, admitted=sum(len(applied.accepted) for applied in out.applied))
    if screen is not None:
        _print_end(screen, _masked(out.result, root), fails, said=screen.lines_said)
    else:
        _print(_masked(out.result, root))
    for line in [*already, *(after if screen is None else [])]:  # how many rows of the list wait, after the summary
        _echo(line)
    if fails:
        raise SystemExit(1)


# -- serve and mcp --------------------------------------------------------------------------------------------------

@main.command()
@_root_option
@click.option("--once", is_flag=True, help="Run one pass, then exit.")
@click.option("--yes", is_flag=True, help="On a first run, approve what its list ticks, and none left out, and ask "
              "nothing: the rest wait for cerebro review.")
@click.option("--see-first", "see_first", default="", metavar="TEXT",
              help="On a first run, what you want to see first, in your own words: the curators and the list's order "
                   "and ticks follow it. At a terminal the run asks for it.")
@click.option("--plain", is_flag=True, help="Print plain lines with no redraw: a pass's progress as a line per step, "
              "and a first run's list as plain lines that ask to accept or quit.")
@click.option("--port", type=int, default=None, help="The MCP port. The default is settings.yaml's port, or 4747.")
@click.option("--host", default=None,
              help="The address to serve on. The default is settings.yaml's serve: host:, else both loopbacks, "
                   "127.0.0.1 and ::1. Another than loopback needs a token made first, and something in front that "
                   "ends TLS.")
@click.option("--allowed-host", "allowed", multiple=True, metavar="NAME",
              help="A Host name this workspace is reached by, matched exactly. Repeat for more. The default is "
                   "settings.yaml's serve: allowed_hosts:, else the loopback names.")
@click.option("--allowed-origin", "origins", multiple=True, metavar="ORIGIN",
              help="An origin whose pages may call the JSON API from a browser, such as https://app.example, matched "
                   "exactly. Repeat for more. The default is settings.yaml's serve: allowed_origins:, else none.")
@_refusing
def serve(root: Path | None, once: bool, port: int | None, host: str | None, allowed: tuple[str, ...],
          origins: tuple[str, ...] = (), yes: bool = False, plain: bool = False, under_init: bool = False,
          see_first: str = "") -> None:
    """Run a pass, then serve the wiki over MCP on localhost. With --once, run one pass and exit.

    A new workspace's first pass proposes what the wiki should hold and writes nothing until you decide: with --once
    at a terminal or with --plain it shows the list and asks, with --yes it approves what the list ticks,
    and none left out, and off a terminal the list waits for cerebro review. With --once, the exit status is 1 when a source could not be read,
    or when something else failed and nothing landed: no article written, no run that ran, no fact stored or
    retracted, and no article the first run's approval admitted; it is 2 when the first run's list waits.
    """
    _null_when_closed()
    if once:
        with open_workspace(root) as workspace, _masked_errors(workspace):
            if not under_init:  # init's run step goes on under the walk's summaries, which name the workspace
                _echo(f"workspace {workspace.root}", cap=None)
            _serve_once(workspace, yes, plain, under_init, see_first)
        return
    early = _settings_first(root)
    taken = [_port_first(early, host, port, allowed, origins)] if early is not None else []
    try:
        with open_workspace(root) as workspace, _masked_errors(workspace):
            _echo(f"workspace {workspace.root}", cap=None)
            if see_first:
                _echo(SEE_FIRST_UNUSED)
            _serving(workspace, taken, host, port, allowed, origins)
    finally:
        for sockets in taken:
            if sockets is not None:
                sockets.close()  # the server has let them go already; a refusal before it ran leaves them here


def _null_when_closed() -> None:
    """Standard output or error closed, as a service started with `>&-` or a supervisor with none has them: each is
    opened on the null device, so serve serves and its lines go nowhere."""
    for fd, name in ((1, "stdout"), (2, "stderr")):
        if getattr(sys, name) is None:
            null = os.open(os.devnull, os.O_WRONLY)
            if null != fd:
                os.dup2(null, fd)
                os.close(null)
            setattr(sys, name, open(fd, "w", buffering=1, closefd=False))  # noqa: SIM115 - the process's own


@contextlib.contextmanager
def _masked_errors(workspace: Cerebro) -> Iterator[None]:
    """Standard error masked while the workspace is open, and an error no refusal names, which would print its
    traceback raw, printed with its traceback masked and exit 1. A refusal's own line is masked likewise."""
    kept = _masking_stderr(workspace)
    try:
        yield
    except click.exceptions.Exit:  # an exit with its own code, such as Ctrl-C's 130, which said its own line
        raise
    except (CerebroError, click.ClickException) as error:
        if isinstance(error, click.ClickException):
            error.message = workspace.mask_every()(error.message)
        else:
            error.args = (workspace.mask_every()(str(error)),)
        raise
    except Exception:  # noqa: BLE001 - printed whole, masked, where Python would print it raw
        import traceback

        sys.stderr.write(traceback.format_exc())
        raise SystemExit(1) from None
    finally:
        sys.stderr = kept


SEE_FIRST_UNUSED = "note      --see-first applies to a workspace's first run only: it was not used"


def _serve_once(workspace: Cerebro, yes: bool, plain: bool, under_init: bool = False, see_first: str = "") -> None:
    """`serve --once`: the first run's list, or one pass. Before a first run's discovery begins, at a terminal, it
    asks what the person wants to see first, which `see_first` gives off one. A pass that fails with an error says so
    in one line, masked as every line is, and exits 1."""
    from . import first_run

    asking = _asking() and not plain and not yes
    if see_first and not first_run.begins(workspace):
        _echo(SEE_FIRST_UNUSED)
    if (see_first or _asking() and not yes) and first_run.begins(workspace) and (
            words := see_first or _see_first(under_init)):
        first_run.see_first(workspace, words)
    screen = _first_run_screen(workspace, plain, region=under_init)
    if screen is not None and under_init:  # a blank line under the Ready block sets the run apart
        screen.p.line("")
    told, failures = _first_failures(_echo if screen is None else screen.say)
    choose, ask_more = _screen(yes, plain), _ask_more if asking else None
    if screen is not None:  # a list or a question takes the screen once what is live there has gone
        choose = _settled(screen, choose, drawn=not yes and asking)
        ask_more = _settled(screen, ask_more)
    try:
        workspace.files_read()  # the first run too stops on a workspace.md or profile.md that does not read
        try:
            out = first_run.run(workspace, choose=choose, say=told, ask_more=ask_more, on=screen)
        except KeyboardInterrupt:
            _stopped(screen, workspace)
        except BaseException:  # what failed before it stopped is said all the same
            if screen is not None:
                screen.finish()
            for what, why in failures.items():
                _echo(f"failed    {what}: {why}")
            raise
        if out is None:
            if screen is not None:
                screen.finish()
            for what, why in failures.items():
                _echo(f"failed    {what}: {why}")
        try:
            result = None if out is not None else _pass_once(workspace, plain) if screen is None else _pass_once(workspace, plain, screen)
        except KeyboardInterrupt:
            _stopped(screen, workspace)
    except (CerebroError, click.ClickException, click.Abort, click.exceptions.Exit, SystemExit, KeyboardInterrupt):
        raise
    except Exception as error:  # noqa: BLE001 - said in one masked line, as serve says a pass that failed
        if screen is not None:
            screen.finish()
        _echo(_failed_pass(workspace, error))
        raise SystemExit(1) from None
    if out is not None:
        if screen is not None and out.result is not None and not out.quit:
            _print_first_run(out, workspace, failures, screen=screen)
        else:
            if screen is not None:
                screen.finish()
            _print_first_run(out, workspace, failures)
        if out.result is not None and _idle(workspace, out.result):
            raise SystemExit(1)
        return
    assert result is not None
    if screen is not None:
        _print_end(screen, _masked(result, workspace.root), _pass_fails(result, workspace.agents))
    else:
        _print(_masked(result, workspace.root))
    if _pass_fails(result, workspace.agents) or _idle(workspace, result):
        raise SystemExit(1)


def _stopped(screen: Any, workspace: Cerebro) -> None:
    """Ctrl-C ended the run: every model call under way is cancelled and none starts, as serve's stop does, and the
    leases this process holds are given back, so nothing of the run keeps it going; what is live is left whole, as
    it stood, and one line says it stopped and how to go on, then the command exits 130.
    Nothing else is said: no traceback."""
    from .budget import stop as stop_calls
    from .runner import let_go

    stop_calls(workspace.store)
    with contextlib.suppress(Exception):  # a store locked by another command keeps its leases: the next start takes them
        let_go(workspace)
    if screen is not None:
        screen.finish()
    from .output import Printer

    p = screen.p if screen is not None else Printer()
    p.err_line("Stopped. Run cerebro serve --once to go on from where it stopped.")
    raise click.exceptions.Exit(INTERRUPTED)


def _print_end(screen: Any, result: PassResult, fails: bool, said: set[str] = frozenset()) -> None:  # type: ignore[assignment]
    """How a pass ends on a terminal: its region drawn a last time with
    what the pass warned of each article threaded under its row, then a table of what it did, then each line the
    region did not show, each warning once on stderr, kept as what to check, and the last line, which names the
    first of them. What the region showed is not said again; off a terminal `_print` says every line."""
    from .output import _wrap

    if screen.broken:  # the live display stopped: the run ends as it does off a terminal
        _print(result)
        return
    # what grounding repaired or dropped is said under its article's row as counts, and
    # each line of it is kept on its run's line in the agent-runs log: none is said again here, nor is it to check
    warnings = [line for line in result.warnings if line not in result.grounded]
    threaded = screen.close([*warnings, *(f"{k}: {v}" for k, v in result.ran.items()),
                             *(f"{k}: {v}" for k, v in result.failed.items())])
    if screen.broken:  # it stopped as the run ended
        _print(result)
        return
    shown = screen.shown_keys() | said
    p = screen.p
    p.table([[label, str(n)] for label, n in (
        ("Sources checked", len(result.listed)), ("Records landed", result.read), ("Facts appended", result.extracted),
        ("Articles written", len(result.compiled)), ("Agents run", len(result.runs))) if n])
    width = p.width()
    for kind, lines in (("refused", result.refused), ("notice", result.notices)):
        for line in lines:
            p.line(f"{kind:<9} " + _wrap(line, 10, width))
    if result.held:
        p.line(f"held: {len(result.held)} agent run{'' if len(result.held) == 1 else 's'} · cerebro status")
    for line in result.stops:
        p.line(_wrap(line, 0, width))
    # a warning no row threads is said on stderr, once, and every one is kept as what to
    # check, so the last line names the first and counts the rest
    for line in warnings:
        if line not in threaded and line not in said:
            p.err_line("cerebro: warning: " + _wrap(line, 18, width))
        p.check(line)
    from .ground import counted

    for key, tally in result.grounding.items():  # under its row already, and to check when it needs a person's eye:
        if key in result.ran or tally.get("citations"):  # an article grounding left with nothing, a citation unread
            p.check(f"{key}: {counted(tally)}")
    for key, why in result.failed.items():
        line = f"{key}: {why}"
        if line not in threaded and key not in shown:
            p.err_line("cerebro: " + _wrap(line, 9, width))
        p.check(line)
    if result.next:
        p.next(result.next)
    elif not fails:
        p.next("cerebro serve, to keep it current")
    # the last line, broken at a space where it passes the terminal's width, as every line of the run's end
    p.err_line(_wrap(p.last_line(failed=fails, run=result.next or "cerebro serve --once"), 2, width))


def _first_run_screen(workspace: Cerebro, plain: bool, region: bool = False) -> Any:
    """A first run's live screen: its discovery on live steps and its build in the pass's region, on a
    terminal that takes styles; None off one, with --plain or NO_COLOR, where its plain lines print as they land."""
    if plain or not sys.stdout.isatty() or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return None
    from .output import Printer
    from .progress import FirstRunScreen

    printer = Printer()
    if not printer.styled_out:
        return None
    width = (workspace.settings.get("compile") or {}).get("concurrency", 8)
    return FirstRunScreen(printer, sources=sorted(workspace.connectors),
                          limit=width if isinstance(width, int) and width > 0 else 8, region=region)


def _settled(screen: Any, ask: Callable[..., Any] | None, drawn: bool = False) -> Callable[..., Any] | None:
    """`ask`, which shows the person something, once what is live on `screen` has gone. `drawn` says a list with rows
    is drawn on a screen of its own, which clears it when it is left; else what `ask` shows is printed."""
    if ask is None:
        return None

    def asked(*args: Any) -> Any:
        screen.settle()
        try:
            return ask(*args)
        finally:
            from .first_run import Pile

            pile = args[0] if args else None
            screen.list_shown(drawn and isinstance(pile, Pile) and bool(pile.rows()))
    return asked


def _failed_pass(workspace: Cerebro, error: BaseException) -> str:
    """The line a pass that raised is logged with, masked of every value the workspace's secrets store holds and of
    any token's shape."""
    return f"failed    the pass: {type(error).__name__}: {workspace.mask_every()(str(error))}"


def _settings_first(root: Path | None) -> dict[str, Any] | None:
    """The settings of the workspace `serve` opens, read before it is opened, or None when its owner does not trust
    it or they do not read: the open refuses it then, in its own words."""
    from .project import read_settings

    found = Path(root if root is not None else Path.cwd()).resolve()
    try:
        if not trusting.trusted(found):
            return None
        return read_settings(found, trusted=True)  # checked as every command checks it, each fault with its line
    except (CerebroError, OSError, UnicodeDecodeError):
        return None


def _port_first(settings: dict[str, Any], host: str | None, port: int | None, allowed: tuple[str, ...],
                origins: tuple[str, ...]) -> Any:
    """The port `serve` answers on, taken before the workspace is opened, before any
    work: a port that cannot be taken is refused with nothing of the workspace written. What settings.yaml and
    the flags alone refuse is refused first, before anything is bound. An address other than loopback is not taken
    here but once the workspace is opened, which says whether it has a live token: none is bound reachable from
    another machine before that is known."""
    from .mcp import take_port
    from .project import check_settings
    from .tokens import allowed_hosts, allowed_origins, is_loopback

    check_settings(settings)
    given, at = _where_served(settings, host, port)
    allowed_hosts(allowed, settings)
    allowed_origins(origins, settings)
    return take_port(given, at) if given is None or is_loopback(given) else None


def _where_served(settings: dict[str, Any], host: str | None, port: int | None) -> tuple[str | None, int]:
    """The host `serve` was given, None for the loopbacks, and the port it answers on, each from its flag, else
    settings.yaml's serve: block, else the defaults."""
    from .mcp import port_of

    kept = settings.get("serve") or {}  # serve: host: and port:, each where its flag is not given
    given = host or kept.get("host")
    if given is not None and not isinstance(given, str):
        raise click.ClickException("serve.host in settings.yaml is not an address: write it as host: 127.0.0.1")
    if port is None and kept.get("port") is not None:
        port = kept["port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise click.ClickException("serve.port in settings.yaml is not a port: write it as port: 4747")
    return given, port_of(settings, port)


def _serving(workspace: Cerebro, holder: list[Any], host: str | None, port: int | None, allowed: tuple[str, ...],
             origins: tuple[str, ...]) -> None:
    """`serve` once its workspace is open: the passes and the HTTP server, until a signal stops them. `holder` holds
    the port `_port_first` took, or takes it here, after the refusals. The first signal stops it:
    every model call of the pass under way is cancelled at once and the pass ends by its own road, each open
    request has `STOP_WAIT` seconds to answer, and past `JOIN` seconds from the signal the process ends whatever is
    left. A second signal ends the process by that signal, and the next start takes over the leases it held."""
    from .budget import stop as stop_calls
    from .mcp import STOP_WAIT, addresses, take_port
    from .mcp import serve as serve_mcp
    from .tokens import LOOPBACK_NAMES, allowed_hosts, allowed_origins, is_loopback

    given, at = _where_served(workspace.settings, host, port)
    host = given or "127.0.0.1"
    live = workspace.may_serve(host)  # refuses another address than loopback with no live token, before any bind
    names = allowed_hosts(allowed, workspace.settings)  # refuses a pattern such as *, likewise
    pages = allowed_origins(origins, workspace.settings)  # refuses * for an origin, likewise
    if not holder or holder[0] is None:
        holder[:] = [take_port(given, at)]
    taken = holder[0]
    bound = taken.getsockname()[1]  # the port asked for, or the one picked for port 0
    stop, served, server, began = threading.Event(), threading.Event(), [], []
    interval = float((workspace.settings.get("serve") or {}).get("interval", INTERVAL))
    loop = threading.Thread(target=run_forever, name="cerebro-passes", daemon=True, kwargs={
        "workspace": workspace, "interval": interval, "stop": stop.is_set, "sleep": stop.wait,
        "on_pass": _passes(workspace), "say": _echo,
        # on standard output with every other line of the log
        "on_error": lambda error: _echo(_failed_pass(workspace, error))})
    last = threading.Lock()

    def left() -> float:
        """Seconds left of the stop's bound, `JOIN` from its signal."""
        return max(0.0, began[0] + JOIN - time.monotonic()) if began else JOIN

    def say(line: str, within: float = 1.0) -> None:
        """A line of the stop, which never keeps the process from ending: a reader gone or stalled holds it `within`
        seconds at most."""
        writer = threading.Thread(target=lambda: _echo_or_not(workspace.mask_every()(line)), daemon=True)
        writer.start()
        writer.join(within)

    def cut() -> list[str]:
        return _cut_lines(*server[0].tally()) if server else []

    def ending(code: int, first: str = "") -> None:
        """End the process now with `code`. The store's lock is taken first and kept, so no thread of this process
        writes to the store from here: what each request cut short wrote is counted then, the leases this process
        holds are given back, and the workspace is closed. A store another command holds locked does not keep the
        stop from ending: its leases are left for the next start, which takes them over."""
        last.acquire()  # one ending: another that comes after waits for the process to end
        under_way = loop.is_alive()
        lines: list[str] = []
        done, gave = threading.Event(), []

        def hold() -> None:
            lock = getattr(workspace.store, "_lock", None)
            if lock is not None:
                lock.acquire()  # never let go: the process ends holding it
            lines.extend(cut())
            gave.append(let_go(workspace))
            done.set()
            workspace.close()

        threading.Thread(target=hold, name="cerebro-ending", daemon=True).start()
        done.wait(max(0.3, left()))
        if under_way and gave:
            said = (f"stopped   a pass was still under way after {JOIN:g} seconds: cerebro "
                    + (f"gave back its {_count(gave[0], 'lease')} and " if gave[0] else "")
                    + "stopped without waiting for it")
        elif under_way or not done.is_set():
            said = (f"stopped   {'a pass was still under way after ' + format(JOIN, 'g') + ' seconds, and ' if under_way else ''}"
                    "the store is locked by another command, so any lease this process holds is left: the next start "
                    "takes them over")
        else:
            said = ""
        for line in [x for x in (first, said) if x] + (lines if done.is_set() else cut()):
            say(line, 0.2)
        _exit_now(code, 0.2)

    def watch() -> None:
        """The stop's own thread: it cancels every model call at once, and ends the process when the stop's bound
        is reached with something still under way, or when the pass is done and the server is still held by a tool
        call that went on past the requests' wait, as a call the stop cannot cancel does."""
        stop_calls(workspace.store)
        while not served.is_set():
            now = time.monotonic()
            if now >= began[0] + JOIN:
                ending(130 if loop.is_alive() else 0)
            if not loop.is_alive() and now >= began[0] + STOP_WAIT + 1:
                ending(0)
            served.wait(0.05)

    def begin(running: Any = None) -> None:
        """The stop, from the first signal: no pass starts after the one under way, which is cancelled."""
        if running is not None:
            server.append(running)
        if not stop.is_set():
            began.append(time.monotonic())
            stop.set()
            threading.Thread(target=watch, name="cerebro-stop", daemon=True).start()

    def stopped(signum: int, frame: Any) -> None:
        if stop.is_set():  # a second signal ends the process by that signal
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        begin()
        raise SystemExit(0)

    previous = {number: signal.signal(number, stopped) for number in (signal.SIGTERM, signal.SIGINT)}
    try:  # from the handlers on, a stop always reaches the finally below: nothing started is left running
        loop.start()
        _echo(f"serving   MCP on {'localhost' if host == '127.0.0.1' else host}, a pass every {interval:g} seconds. "
              "Ctrl-C stops.")
        where = addresses(workspace.settings, host, bound)
        _echo(f"mcp       {where['mcp']}")
        _echo(f"api       {where['api']}, the JSON API")
        _echo(f"health    {where['health']}, which answers with no token")
        if live:
            _echo("tokens    every request needs a token: cerebro tokens list shows them")
        else:
            _echo("tokens    this workspace has no live token, so every HTTP caller is refused until one is made with "
                  "cerebro tokens new <name>")
        if names != LOOPBACK_NAMES:
            _echo(f"hosts     a request must name one of these in its Host: {', '.join(names)}")
        if pages:
            _echo(f"origins   a page from one of these may call /api/v1: {', '.join(pages)}")
        if not is_loopback(host):
            _echo("note      cerebro does not end TLS: serve this address only behind something that does")
            if names == LOOPBACK_NAMES:
                _echo("note      only a loopback Host is accepted: list each name this workspace is reached by with "
                      "--allowed-host <name>")
        asked = port if port is not None else (workspace.settings.get("serve") or {}).get("port")  # the server answers on `taken`
        if origins:
            serve_mcp(workspace, asked, host, allowed or None, origins, taken=taken, on_stop=begin)
        else:  # settings' origins are read by the server itself
            serve_mcp(workspace, asked, host, allowed or None, taken=taken, on_stop=begin)
    except SystemExit as ended:
        if not began or ended.code not in (None, 0):
            raise
    finally:
        failed = not began  # the server ended with no signal: an error is on its way out, never a stop's status
        if failed:
            began.append(time.monotonic())
            stop_calls(workspace.store)
        why = _serve_failed(workspace, sys.exc_info()[1]) if failed else ""
        stop.set()  # the loop ends with the pass under way, its model calls cancelled, before the workspace closes
        served.set()
        loop.join(left())
        if loop.is_alive():
            ending(1 if failed else 130, why)  # the pass outlasted its grace: the process ends now
        if server and server[0].calls.running():
            ending(1 if failed else 0, why)  # a tool call the stop could not cancel, its caller cut off already
        for number, handler in previous.items():  # after a stop, a further signal ends the process by itself
            ending_by = not failed and handler in (signal.SIG_DFL, signal.default_int_handler)
            signal.signal(number, signal.SIG_DFL if ending_by else handler)
    lock = getattr(workspace.store, "_lock", None)
    if lock is not None and not lock.acquire(timeout=max(0.3, left())):
        ending(0)  # a thread the stop could not end is in the store, as a request waiting on another command's lock
    elif lock is not None:
        lock.release()
    for line in cut() or ["stopped   no request is open and no pass is under way"]:
        say(line)


def _serve_failed(workspace: Cerebro, error: BaseException | None) -> str:
    """The line serve ends with when an error, not a signal, ended its server: masked, as every line is."""
    if error is None or isinstance(error, SystemExit):
        return ""
    return f"failed    serve: {type(error).__name__}: {workspace.mask_every()(' '.join(str(error).split()))}"


class _Masking:
    """Standard error with every value the workspace's secrets store holds, and each token's shape, masked in what
    is written to it: a traceback or a library's log line says what an error said, which may hold one."""

    def __init__(self, stream: Any, mask: Callable[[str], str]) -> None:
        self.stream, self.mask = stream, mask

    def write(self, text: str) -> int:
        self.stream.write(self.mask(text))
        return len(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def _masking_stderr(workspace: Cerebro) -> Any:
    """Mask what is written to standard error from here, and give what to put back."""
    from .tokens import hide_tokens

    kept = sys.stderr
    if kept is not None:
        every = workspace.mask_every
        sys.stderr = _Masking(kept, lambda text: hide_tokens(every()(text)))  # type: ignore[assignment]
    return kept


def _cut_lines(nothing: int, written: int) -> list[str]:
    """The lines that say which requests a stop cut short: those that wrote nothing, and those that had written."""
    from .mcp import STOP_WAIT

    lines = []
    if nothing:
        lines.append(f"stopped   {_count(nothing, 'request')} still open after {STOP_WAIT:g} seconds cut short: "
                     f"{'its caller' if nothing == 1 else 'each caller'} got no answer, and nothing of "
                     f"{'it' if nothing == 1 else 'them'} was written")
    if written:
        lines.append(f"stopped   {_count(written, 'request')} still open after {STOP_WAIT:g} seconds got no answer, "
                     f"though what {'it' if written == 1 else 'they'} wrote stands")
    return lines


def _echo_or_not(line: str) -> None:
    try:
        _echo(line)
    except Exception:  # noqa: BLE001, S110 - standard output closed: the stop goes on regardless
        pass


def _pass_once(workspace: Cerebro, plain: bool, screen: Any = None) -> PassResult:
    """One pass of `serve --once`. On a terminal that takes styles, its progress is one region drawn again in place,
    on `screen` when one is given, which keeps it to end the run on; off one, with --plain or NO_COLOR, it prints its
    plain lines as each step happens."""
    if screen is not None:
        from .runner import Progress

        screen(Progress("build", "start"))
        try:
            return workspace.run_once(on=screen)
        finally:
            screen(Progress("build", "end"))
    if plain or not sys.stdout.isatty() or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return workspace.run_once(say=_echo)
    from .output import Printer
    from .progress import LiveProgress

    printer = Printer()
    if not printer.styled_out:
        return workspace.run_once(say=_echo)
    width = (workspace.settings.get("compile") or {}).get("concurrency", 8)
    screen = LiveProgress(printer, limit=width if isinstance(width, int) and width > 0 else 8)
    screen.begin()
    try:
        return workspace.run_once(on=screen)
    finally:
        screen.finish()


def _exit_now(code: int = 0, within: float = 1.0) -> None:
    """End the process at once, so no thread of a pass or a request cut short writes to a workspace being closed.
    Output that cannot be flushed in `within` seconds, its reader gone or stalled, never keeps the process from
    ending."""
    flushing = threading.Thread(target=lambda: [_flushed(stream) for stream in (sys.stdout, sys.stderr)], daemon=True)
    flushing.start()
    flushing.join(within)
    os._exit(code)


def _flushed(stream: Any) -> None:
    try:
        stream.flush()
    except Exception:  # noqa: BLE001, S110 - the process ends whatever its output does
        pass


# -- tokens --------------------------------------------------------------------------------------------------------

@main.group()
def tokens() -> None:
    """Tokens for the HTTP server. Each names a caller, which sends it as Authorization: Bearer <token>."""


@tokens.command("new")
@click.argument("name")
@_root_option
@_refusing
def tokens_new(name: str, root: Path | None) -> None:
    """Make a token for the caller NAME and print it, this once. The workspace keeps only its hash."""
    from .caller import describe

    with open_workspace(root) as workspace:
        made = workspace.make_token(name)
    _echo(made.token)
    _echo(f"made     {describe(made.record.caller)}, at {made.record.made}: the token above is shown this once, "
               "and the workspace keeps only its hash")


@tokens.command("list")
@_root_option
@_json_option
@_refusing
def tokens_list(root: Path | None, as_json: bool) -> None:
    """The live tokens: each one's name, when it was made and last used. Never a token."""
    with open_workspace(root) as workspace:
        live = workspace.tokens()
    if as_json:
        _print_json([_data(token) for token in live])
        return
    for token in live:
        _echo(f"{token.name}  made {token.made}  last used {token.used or 'never'}")
    _echo(_count(len(live), "token") + ("" if live else ": make one with cerebro tokens new <name>"))


@tokens.command("revoke")
@click.argument("name")
@_root_option
@_refusing
def tokens_revoke(name: str, root: Path | None) -> None:
    """End the token of NAME: the next request that carries it is refused. The name may be made again."""
    with open_workspace(root) as workspace:
        workspace.revoke_token(name)
        last = not workspace.tokens()
    _echo(f"revoked  {name}: the next request that carries its token is refused")
    if last:
        _echo("note     that was the last live token: the HTTP server now refuses every request until a new one "
                   "is made with cerebro tokens new <name>")


@main.command()
@click.option("--source", "source", default=None, metavar="NAME", help="Check this source alone.")
@_root_option
@_refusing
def distill(source: str | None, root: Path | None) -> None:
    """Check the sources now, or the one --source names, whatever their freshness window, and extract facts from
    what they list. No agent runs."""
    with open_workspace(root) as workspace:
        _echo(f"workspace {workspace.root}", cap=None)
        from .first_run import distill_line

        result = workspace.run_once(only=source, agents=False)
        _print(_masked(result, workspace.root), agents=False)
        if waiting := distill_line(workspace):
            _echo(f"waiting   {waiting}")
        if result.failed and not result.extracted or _idle(workspace, result):
            raise SystemExit(1)


@main.command()
@_root_option
@_refusing
def mcp(root: Path | None) -> None:
    """Serve the wiki over MCP on standard input and output, for a client that starts it on demand."""
    from .mcp import serve_stdio

    with open_workspace(root) as workspace:
        _echo(f"workspace {workspace.root}", err=True, cap=None)  # standard output carries the protocol
        serve_stdio(workspace)


# -- version ----------------------------------------------------------------------------------------------------------

@main.command("version")
def version_command() -> None:
    """Print the installed version."""
    from . import __version__

    build = _build()
    _echo(f"cerebro {__version__}" + (f" ({build})" if build else ""), cap=None)


def _build() -> str:
    """The commit this code runs from, as `git describe --tags --always --dirty` names it, when it runs in place
    from this project's own checkout; "" otherwise."""
    package = Path(__file__).resolve().parent
    if not _is_own_checkout(package):
        return ""
    try:
        done = subprocess.run(["git", "-C", str(package), "describe", "--tags", "--always", "--dirty"],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ''


def _is_own_checkout(package: Path) -> bool:
    """Whether the package runs in place from src/ of this project's own checkout: an install inside another
    repository must not name that repository's commit."""
    root = package.parent.parent
    if package != root / "src" / "cerebro" or not (root / ".git").exists():
        return False
    try:
        return tomllib.loads((root / "pyproject.toml").read_text()).get("project", {}).get("name") == "cerebro"
    except (OSError, tomllib.TOMLDecodeError):
        return False
