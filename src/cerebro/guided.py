"""The guided `init`: at a terminal that draws, with no flag, `cerebro init` walks a person through setting a
workspace up: who they are and their team or organization, asked before the workspace
is laid down, then the models, the model keys, the sources, the MCP clients and the first run. Run again on a
workspace, it shows what is set, asks only for a work email profile.md lacks, and offers the rest.

Each step is a few questions and one write. Nothing is written until a step's answer is whole, and each file is
written whole, so Escape, an empty answer or input that ends leaves the step as it was, and a later `init` or
`connect` picks it up. Every step can be passed over.

The walk asks through `Screen`, a small interface: a menu, a line of text, a pager and a line said. The terminal's
screen draws with the checklist module, imported only when it draws; a test passes a scripted one. The sources step
is `connect`'s own lobby, and the first run is `serve --once` with its own list: the command passes both in. The MCP
clients' config files come from `mcp_clients.clients`, given the home folder the command passes.

What the walk asks is the engine's alone. A question that only makes sense for one use of Cerebro, such as a role,
belongs to a template, not here.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from . import keys as key_lobby
from . import mcp_clients, scaffold
from .agent import FRONT, read_yaml
from .errors import CerebroError
from .evidence import RECENT_DAYS
from .models import JOBS, PRICES, PROVIDERS, ModelSettingsError, ModelSpec, _is_rune, read_model, read_models
from .project import _identity_list
from .shown import shown

if TYPE_CHECKING:
    from .checklist import MenuList
    from .output import Printer

__all__ = ["Screen", "TerminalScreen", "Walk", "clients_menu", "models_menu",
           "template_menu"]

CAP = 120  # the most characters of a name, a domain or a model a line shows
PROFILE_BODY = ("\nWho you are, in a few lines. Cerebro reads your addresses above to tell your side from everyone "
                "else's.\n")
SHORT = "accounts/fireworks/models/"  # a Fireworks model's id as settings.yaml names it; a router keeps its full id


class Screen(Protocol):
    """How the walk asks. `menu` gives the place of the row Enter opened, or None for Escape or the end row; `text`
    gives the line typed, or None when input ends; `read` shows lines until Escape; `say` prints a line."""

    def menu(self, menu: MenuList, cursor: int = 0) -> int | None: ...

    def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = "") -> str | None: ...

    def read(self, lines: Sequence[str]) -> None: ...

    def say(self, line: str) -> None: ...


def prompt_line(prompt: str, default: str = "", hidden: bool = False, source: str = "") -> str:
    """A line asked, as the terminal shows it before its colon: the prompt, then its default and where that comes
    from, as in `Team or organization name [Quillmoor, from your email's domain]`, and no default on a hidden line."""
    return prompt + (f" [{default}{', ' + source if source else ''}]" if default and not hidden else "")


class _Ended(BaseException):
    """SIGTERM, SIGHUP or SIGQUIT while a view is drawn apart: a BaseException, so no `except Exception` inside the
    view takes it for a failed step, and the view's screen comes back before the command ends with `code`."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _ended(signum: int, frame: Any) -> None:
    import signal

    raise _Ended({signal.SIGTERM: 130, signal.SIGHUP: 129}.get(signum, 128 + signum))


@contextmanager
def held_terminal() -> Iterator[None]:
    """The terminal kept whole while a hidden line is read: Ctrl-\\ and Ctrl-Z reach the line as keys, read
    in raw mode, and so stop nothing; SIGTERM, SIGHUP or SIGQUIT puts the modes back, then
    ends the command with 130, 129 or 131; and the modes as they were come back on every way out. Nothing of it off a
    terminal."""
    import signal

    import click

    modes = _modes()
    if modes is None:
        yield
        return
    keys = list(modes)
    keys[6] = list(modes[6])
    import termios

    try:
        off = os.fpathconf(sys.stdin.fileno(), "PC_VDISABLE")
    except (OSError, ValueError):
        off = 0
    for name in ("VQUIT", "VSUSP", "VDSUSP"):
        if hasattr(termios, name):
            keys[6][getattr(termios, name)] = off if isinstance(keys[6][0], int) else bytes([off])
    kept = {sig: signal.signal(sig, _ended) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}
    try:
        _restore(keys)
        yield
    except _Ended as ended:
        raise click.exceptions.Exit(ended.code) from None
    finally:
        for sig, handler in kept.items():
            signal.signal(sig, handler)
        _restore(modes)


def _modes() -> Any:
    """The terminal's modes as stdin's terminal holds them now, or None off one."""
    try:
        import termios
    except ImportError:
        return None
    try:
        return termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, OSError, ValueError, AttributeError):
        return None


def _restore(modes: Any) -> None:
    if modes is None:
        return
    import termios

    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, modes)
    except (termios.error, OSError, ValueError, AttributeError):  # the terminal is gone, or stdin closed
        pass


class TerminalScreen:
    """The walk at a terminal: menus and the pager drawn inline with the checklist module, and lines asked with a
    plain prompt, a key's hidden as it is typed."""

    def __init__(self, printer: Printer | None, echo: Callable[[str], None]) -> None:
        from .connect_screen import Apart

        self.printer, self.echo = printer, echo
        self.ended = False  # a line met the end of input, as Ctrl-D: init's own questions ask nothing after it
        self.drawn = printer is not None and printer.styled_out and printer.styled_err
        self.alt = Apart(printer if self.drawn else None)  # a step's lobby and views, drawn apart
        self.rows = 0  # the rows the questions asked so far take, which `erase` takes away

    def menu(self, menu: MenuList, cursor: int = 0) -> int | None:
        from .checklist import MENU_DONE, choose

        if self.alt.on:
            self.alt.take()  # each menu of a step apart draws from the top of a clear screen
        got = choose(menu, self.printer, cursor)
        return None if got == MENU_DONE else got

    @contextmanager
    def step_apart(self) -> Iterator[None]:
        """A step's lobby and its views on the alternate screen, and the main screen as it was
        once the step ends, by any way out; its summary then follows on the main screen. Nothing of it where
        the terminal does not draw."""
        try:
            self.alt.take()
            yield
        finally:
            self.alt.leave()

    def _count(self, line: str) -> None:
        import shutil

        from .output import visible_width

        width = max(shutil.get_terminal_size().columns, 1)
        self.rows += max(1, -(-visible_width(line) // width))

    def erase(self) -> None:
        """Take the questions asked so far off the screen, for the summary to take their place, where
        the terminal draws and they are all still on it."""
        import shutil

        rows, self.rows = self.rows, 0
        if not self.drawn or rows <= 0 or rows >= shutil.get_terminal_size().lines:
            return
        sys.stdout.write(f"\x1b[{rows}A\r\x1b[J")
        sys.stdout.flush()

    def summary(self, mark: Any, line: str, hint: str = "") -> None:
        """A step's summary: a blank line, the mark and the line, then the hint, muted, two spaces
        in, its commands in the accent."""
        p = self.printer
        assert p is not None
        from .output import fold

        p.line("")
        for row in fold(p.mark(mark) + " " + line, p.width(), "  "):
            p.line(row)
        if hint:
            for row in fold("  " + _muted_commands(p, hint), p.width(), "  "):
                p.line(row)

    def muted(self, line: str) -> None:
        """A muted line on its own after a blank line, such as why the first run waits."""
        from .output import fold

        p = self.printer
        assert p is not None
        p.line("")
        for row in fold(_muted_commands(p, line), p.width()):
            p.line(row)

    def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = "") -> str | None:
        """The line typed, or the default for an empty one; None when input ends with nothing typed. Text typed
        before the input ends, as with Ctrl-D, is kept whole, and the input stays
        ended, so nothing more is asked. Ctrl-C ends the command with 130."""
        import click

        if hidden and self.drawn:  # a key's view prompt: the legend under it, Escape back, Ctrl-C ends
            from .checklist import FieldAsk, FieldBack, NoRawKeys, field

            try:
                return field(FieldAsk(prompt, hidden=True), self.printer)
            except FieldBack:
                return "\x1b"  # Escape alone, which the key's view reads as going back
            except KeyboardInterrupt:
                raise click.exceptions.Exit(130) from None
            except NoRawKeys:
                pass
        if hidden:
            try:
                with held_terminal():  # the modes come back on a signal, and Ctrl-\ and Ctrl-Z are keys
                    return str(click.prompt(prompt_line(prompt, default, hidden, source), default=default,
                                            show_default=False, hide_input=True)).strip()
            except click.Abort as error:
                if isinstance(error.__context__, KeyboardInterrupt):  # Ctrl-C ends the command
                    raise click.exceptions.Exit(130) from None
                self.echo("")  # a key's view goes back, and the walk goes on
                return None
        cut = [False]

        def read(text: str) -> str:
            """A line as the terminal gives it: text typed before the input ends is kept whole, where `input` without
            readline drops its last character and needs more Ctrl-D; off a terminal, click's own reader."""
            if sys.stdin is None or not sys.stdin.isatty():
                return click.termui.visible_prompt_func(text)
            sys.stdout.write(text)
            sys.stdout.flush()
            line = sys.stdin.readline()
            if not line:
                raise EOFError
            cut[0] = not line.endswith("\n")
            return line.rstrip("\r\n")

        try:
            line = click.termui._readline_prompt(read, prompt_line(prompt, default, hidden, source) + ": ", False)
        except KeyboardInterrupt:  # Ctrl-C ends the command
            self.echo("")
            raise click.exceptions.Exit(130) from None
        except EOFError:
            self.echo("")
            self.ended = True
            return None
        self._count(prompt_line(prompt, default, hidden, source) + ": " + line)
        if cut[0]:  # the input ended after text was typed: the text stands, and nothing more is asked
            self.echo("")
            self.ended = True
        return line.strip() or default

    def heading(self, title: str) -> None:
        """A view's heading: bold after a blank line, plain off a style."""
        if self.printer is None:
            self.echo(title)
        elif self.alt.on:  # a view apart starts at the top of its screen, its title first
            self.printer.line(self.printer.bold(title))
        else:
            self.printer.heading(title)

    def note(self, text: str, accent: str = "") -> None:
        """A view's line: muted, with `accent`, where it holds it, in the accent
        colour, on standard error; plain off a style."""
        if self.printer is None:
            self.echo(text)
            return
        if not self.printer.styled_err:
            self.printer.err_line(text)
            return
        from .output import fold

        st = self.printer.st
        before, found, after = text.partition(accent) if accent else (text, "", "")
        line = "".join(st.paint(name, part) for name, part in (("muted", before), ("key", found), ("muted", after))
                       if part) or ""
        for row in fold(line, self.printer.width()):  # wrapped at its spaces, never mid-word
            self.printer.err_line(row)

    def step(self, label: str) -> Any:
        """A step drawn while work runs, one level in: a spinner and the time, two spaces in,
        that ends with a mark; None on a terminal that does not draw."""
        if self.printer is None or not self.printer.styled_out:
            return None
        return self.printer.step(label, level=1)

    def apart(self) -> Any:
        """A view drawn apart from the lobby, on its alternate screen: the
        screen cleared before it, and the main screen and the terminal's modes back when it ends, by any way out, so
        the view is gone and the lobby redraws alone. The ways out: its key, Escape, input that ends, Ctrl-C, an
        error inside it, a closed terminal, and SIGTERM, SIGHUP or SIGQUIT, which end the command with 130,
        129 and 131. Ctrl-\\ and Ctrl-Z are keys in it, and stop nothing. Nothing of it off
        a terminal that draws."""
        printer = self.printer

        @contextmanager
        def screen() -> Any:
            if self.alt.on:  # the lobby is apart already: the view draws from the top of its screen, cleared
                self.alt.take()
                with held_terminal():
                    yield
                return
            if printer is None or not printer.styled_err:
                yield
                return
            try:
                with held_terminal():
                    printer.err.write("\x1b[?1049h\x1b[H\x1b[2J")
                    printer.err.flush()
                    yield
            finally:
                try:
                    printer.err.write("\x1b[?1049l")
                    printer.err.flush()
                except (OSError, ValueError):  # the terminal is gone: there is no screen to bring back
                    pass

        return screen()

    def read(self, lines: Sequence[str]) -> None:
        from .checklist import read

        read(list(lines), self.printer)

    def say(self, line: str) -> None:
        self._count(line)
        self.echo(line)

    def styled(self, line: str, indent: str = "") -> None:
        """A line the walk drew itself, its styles kept: the printer's own, never the echo, which shows a control
        character as text; wrapped at its spaces to the terminal's width, the rows after the first set in by
        `indent`."""
        from .output import fold

        if self.printer is None:
            self._count(line)
            self.echo(line)
            return
        for row in fold(line, self.printer.width(), indent):
            self._count(row)
            self.printer.line(row)


def _muted_commands(p: Printer, line: str) -> str:
    """A line muted, each `cerebro ...` command in it, and each file or folder it names, in the accent."""
    import re

    parts = re.split(r"(cerebro [a-z-]+(?: [a-z<>-]+){0,2}|sources/)", line)
    return "".join(p.key(part) if i % 2 else p.dim(part) for i, part in enumerate(parts) if part)


# -- the menus each step shows, built from what the step read, so a frame shows what a person sees


def _menu(title: str, intro: str, rows: list[Any], status: Sequence[str] = (), end: str = "Done") -> MenuList:
    from .checklist import MenuList

    return MenuList(rows=rows, head=[title, intro, *([""] + list(status) if status else [])], end=end)


def template_menu(names: Sequence[str]) -> MenuList:
    """What a new workspace starts from: blank, or a bundled template's agents."""
    from .checklist import MenuRow

    rows = [MenuRow("blank", "no agent", "you write the first one")]
    rows += [MenuRow(shown(name, CAP), "template", f"starts with the {shown(name, CAP)} template's agents")
             for name in names]
    menu = _menu("New workspace", "Start from a blank workspace or a template. Escape writes nothing.", rows, end="")
    menu.open = "select"  # Enter selects, as every menu of the walk says it
    return menu


# The jobs as Model selection names them: the title, what the job does, and the line its pickers say.
ACTIVITIES = {
    "extraction": ("Extraction", "extracts facts from source records",
                   "The extraction model extracts facts from source records."),
    "decision": ("Decision", "classifies, decides, and evaluates",
                 "The decision model supports classification and evaluation steps used throughout the system."),
    "compilation": ("Compilation", "compiles facts into articles", "The compilation model compiles facts into articles."),
}


def _dollars(v: float) -> str:
    """v in dollars, with two decimals or as many more as it needs."""
    whole, _, frac = f"{v:f}".rstrip("0").partition(".")
    return f"${whole}.{frac.ljust(2, '0')}"


def _price(spec: ModelSpec) -> str:
    """A model's list price per million tokens, such as `$1.40 in, $4.40 out per million tokens`."""
    price = PRICES.get((spec.provider, spec.wire_id))
    if price is None:
        return "no list price"
    out = _dollars(price.input) + " in" + (f", {_dollars(price.output)} out" if price.output else "")
    return out + " per million tokens"


def _named(spec: ModelSpec) -> str:
    return f"{spec.provider} {shown(spec.model, CAP)}" + (f", effort {spec.effort}" if spec.effort else "")


def models_menu(specs: Mapping[str, ModelSpec], status: Sequence[str] = (),
                mark: Callable[[Any], str] = lambda m: m.symbol(False)) -> MenuList:
    """Model selection: one row a job, its title, the provider and model it runs on, and what the job does, then
    Done. Enter changes a job's model."""
    from .checklist import MenuList, MenuRow
    from .facts import FACTOID_DECISION
    from .output import Mark

    # extraction decides on the facts it finds with the facts layer's own model, whatever the Decision row names, and
    # the keys step asks for that provider's key: the row says so when no other row names that model
    theirs, ours = specs["decision"], FACTOID_DECISION
    also = (() if (theirs.provider, theirs.model) == (ours.provider, ours.model) else
            (f"and {ours.provider} {ours.model}, which decides on the facts it finds",))
    rows = [MenuRow(ACTIVITIES[job][0], f"{specs[job].provider} {shown(specs[job].model, CAP)}", ACTIVITIES[job][1],
                    mark=mark(Mark.DONE), below=also if job == "extraction" else ()) for job in JOBS]
    return MenuList(rows, ["Model selection", "Choose providers and models for the following processes.", *status],
                    end="Done", open="select")


def _models_of(provider: str, job: str) -> list[ModelSpec]:
    """The models of a provider's list prices that can run a job, by the name settings.yaml takes."""
    out = []
    for p, model in PRICES:
        if p != provider:
            continue
        name = model.removeprefix(SHORT)
        if _is_rune(name) != (job == "decision") and provider == "fireworks":
            continue  # Rune decides, and decides only
        try:
            out.append(read_model({"provider": p, "model": name}, "", job))
        except ModelSettingsError:
            continue
    return list(dict.fromkeys(out))


def providers_menu(job: str) -> tuple[MenuList, list[str]]:
    """The provider picker for a job: `<Job> model provider`, the job's line, a row for each provider, then Done."""
    from .checklist import MenuList, MenuRow

    names = [p for p, v in PROVIDERS.items() if job in v.jobs and _models_of(p, job)]
    title, _, line = ACTIVITIES[job]
    return MenuList([MenuRow(p) for p in names], [f"{title} model provider", line], end="Done", open="select",
                    end_takes=True), names


def model_menu(provider: str, job: str) -> tuple[MenuList, list[ModelSpec]]:
    """The model picker: `<Job> model`, the job's line, a row for each model with its list price, then Done."""
    from .checklist import MenuList, MenuRow

    specs = _models_of(provider, job)
    title, _, line = ACTIVITIES[job]
    return MenuList([MenuRow(shown(s.model, CAP), _price(s)) for s in specs], [f"{title} model", line], end="Done",
                    open="select", end_takes=True), specs


def clients_menu(rows: Sequence[tuple[str, str, str]], status: Sequence[str] = (),
                 mark: Callable[[Any], str] = lambda m: m.symbol(False)) -> MenuList:
    """The MCP clients lobby: its title, a line under it that says how the last client went, then one row a client,
    `installed` when its config holds this workspace's server, else `not installed`, with what is there or why, then
    Done."""
    from .checklist import MenuList, MenuRow
    from .output import Mark

    return MenuList([MenuRow(app, state, shown(reason, CAP), mark=mark(Mark.DONE if state == INSTALLED else
                                                                         Mark.FAILED)) for app, state, reason in rows],
                    ["MCP clients", *status], end="Done", open="select")


INSTALLED, NOT_INSTALLED = "installed", "not installed"
WRITE, UPDATE, REPLACE, SHOW = "Install for me", "Update it", "Replace it", "Let me do it"


def client_menu(app: str, choices: Sequence[str]) -> MenuList:
    """The view of one client: its name, `Add the MCP client for this workspace to <app>.`, and what can be done,
    each only where it applies: install it, update an entry that starts another command, replace another
    workspace's, or the steps to do it by hand. Enter selects; Escape goes back to the clients."""
    from .checklist import MenuList, MenuRow

    return MenuList([MenuRow(choice) for choice in choices], [app, f"Add the MCP client for this workspace to {app}."],
                    open="select", done="back")


def manual_lines(app: str, steps: Sequence[tuple[str, str]], printer: Printer | None) -> list[str]:
    """The manual view of a client: its name in bold, its intro muted, then each numbered step, its text to copy on
    a tint under it, a blank line before each step."""
    intro = {mcp_clients.CLAUDE_CODE: "Add this workspace's MCP server to Claude Code.",
             mcp_clients.CODEX: "Add this workspace's MCP server as an mcp_servers table in Codex's config file."}.get(
        app, f"Add this workspace's MCP server to the mcpServers block in {app}'s config file.")
    if printer is None:  # plain: the numbered steps alone, each block set in under its step
        return [line for n, (do, block) in enumerate(steps, 1)
                for line in [f"{n}. {do}", *(f"   {b}" for b in block.splitlines()), ""]]
    bold, dim = printer.bold, printer.dim
    lines = [bold(app), dim(intro)]
    for n, (do, block) in enumerate(steps, 1):
        lines += ["", f"{n}. {do}"]
        if block:
            lines += printer.tinted(block)
    return lines


READY = (f"This step proposes the initial set of articles for you to approve, reads the last {RECENT_DAYS} days of "
         "your sources for them, and asks once more about what those records name. Then it writes what you approve, "
         "first from recent facts, then kept current: cerebro serve reads the older records on its next passes. What "
         "you leave unticked waits for cerebro review.")


# -- the walk


def _front(path: Path) -> dict[str, Any]:
    try:
        found = FRONT.match(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}
    data, _ = read_yaml(found.group(1)) if found else ({}, None)
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class You:
    """Who you are and the team or organization the workspace is for, as a new workspace's first questions answer it."""

    name: str = ""
    email: str = ""
    org: str = ""
    domains: tuple[str, ...] = ()


GUESSED = "from your email's domain"


SKIP = "Enter to skip"


def ask(screen: Screen, prompt: str, check: Callable[[str], Any], default: str = "", source: str = "") -> str | None:
    """One line asked: the prompt, then its default and where that comes from, or
    `[Enter to skip]` when it has none. Enter takes the default, or none. An answer that does not pass `check` is
    told why and asked again. None when input ends."""
    while True:
        said = screen.text(prompt if default else f"{prompt} [{SKIP}]", default=default,
                           source=source if default else "")
        if said is None:
            return None
        try:
            check(said)
            return said
        except CerebroError as error:
            if getattr(screen, "ended", False):  # it cannot be asked again: init ends with it
                raise
            screen.say(f"          {' '.join(str(error).split())}")


def ask_you(screen: Screen, given: You = You()) -> You:
    """A new workspace's first questions, in this order: your name and work email, then the team or
    organization's name and its web domain, each with the guess from the email's domain as its default, and none for an
    address at a public mail service. What `given` holds, as init's flags give it, is not asked. Each answer is
    checked as the flag's would be, by the same function, and asked again when it does not do. Once the input ends,
    as with Ctrl-D, nothing more is asked, and each question left takes
    no answer: the workspace is laid down with what it has."""
    ended = False

    def asked(prompt: str, check: Callable[[str], Any], default: str = "", source: str = "") -> str:
        nonlocal ended
        said = None if ended else ask(screen, prompt, check, default, source)
        ended = ended or said is None or getattr(screen, "ended", False)
        return said or ""

    name = given.name or asked("Your name", lambda v: scaffold.check_text("name", v))
    email = given.email or asked("Your work email", scaffold.check_email)
    guessed, guess_domain = scaffold.org_guess(email)
    org = given.org or asked("Team or organization name", lambda v: scaffold.check_text("team or organization", v),
                             guessed, GUESSED)
    if given.domains:
        return You(name, scaffold.check_email(email), org, given.domains)
    web = asked("Team or organization web domain", scaffold.check_domain, guess_domain, GUESSED)
    if not web or web == guess_domain:  # the default taken changes nothing: the email's own domain is written
        return You(name, scaffold.check_email(email), org, ())
    own = scaffold.check_domain(web)
    return You(name, scaffold.check_email(email), org, (own,) if own else ())  # an answer is no placeholder


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) < 2 else ", ".join(items[:-1]) + " and " + items[-1]


@dataclass
class Walk:
    """The steps on the workspace at `root`. `lobby` runs `connect`'s lobby there, `first_run` its first pass, and
    `run_tool` a client's own command, or None where it cannot be run. `home` holds the clients' config files, and
    `environ` gives their own variables. `prove` makes a key's proof call, the app's own when None. `new` says the
    workspace was laid down this run, after its first questions were asked, so they are not asked again.

    What still waits when the walk ends, a key not kept or nothing to read, is in `checks`, and the command to run
    next in `next`, for the command's last line."""

    root: Path
    screen: Screen
    open_workspace: Callable[[Path], Any]
    lobby: Callable[[], None]
    first_run: Callable[[], None]
    home: Path
    command: str
    system: str = sys.platform
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    run_tool: Callable[[Sequence[str]], tuple[int, str]] | None = None
    prove: Callable[[key_lobby.KeyRow, str], None] | None = None  # a key's proof call; the app's own when None
    mark: Callable[[Any], str] | None = None  # a mark as the menu's stream draws it
    new: bool = False
    checks: list[str] = field(default_factory=list)
    next: str = ""

    def all(self) -> tuple[list[str], str]:
        """Every step in turn, then what still waits and what to run next."""
        steps = [self.models, self.keys, self.sources, self.clients, self.run]
        for step in steps if self.new else [self.you, self.domains, *steps]:
            step()
        return self.checks, self.next

    def _asking(self) -> bool:
        """Whether a step may ask: not once the input has ended, as with Ctrl-D."""
        return not getattr(self.screen, "ended", False)

    def _write(self, why: str, write: Callable[[], None]) -> bool:
        try:
            write()
        except (CerebroError, OSError) as error:
            self.screen.say(f"skipped   {why}: {' '.join(str(error).split())}")
            return False
        return True

    def you(self) -> None:
        """Run again, who you are: a name profile.md lacks is left for you to
        fill in, and only a missing work email is asked, written alone, by `--email`'s own check."""
        path = self.root / "profile.md"
        identity = _front(path).get("identity") or {}
        name = str(identity.get("name") or "") if isinstance(identity, dict) else ""
        emails = list(_identity_list(path, "email")) if path.is_file() else []
        drawn = self._drawn()  # where the walk draws, the workspace's summary says it: no line of its own
        say = (lambda line: None) if drawn else self.screen.say
        if not name:
            say("you       profile.md has no name: fill it in")
            self.checks.append("profile.md has no name: fill it in")
        if emails:
            if name:
                say(f"you       {shown(name, CAP)} <{shown(emails[0], CAP)}>, in profile.md")
            return
        email = ask(self.screen, "Your work email", scaffold.check_email) if self._asking() else None
        if drawn:
            self.screen.erase()  # type: ignore[attr-defined]
        if not email:
            say("you       profile.md has no email")
            return
        email = scaffold.check_email(email)
        if self._write("you", lambda: scaffold.set_identity(path, "email", [email], body=PROFILE_BODY)):
            say(f"you       {shown(email, CAP)}, added to profile.md")

    def domains(self) -> None:
        """Run again, the workspace's own domains as workspace.md lists them. Nothing is asked."""
        if self._drawn():
            return
        path = self.root / "workspace.md"
        held = list(_identity_list(path, "domains")) if path.is_file() else []
        self.screen.say(f"domains   {shown(', '.join(held), CAP)}, in workspace.md" if held else
                        "domains   none in workspace.md: people at your own domains count as your side once its "
                        "identity: domains: lists them")

    def _specs(self) -> dict[str, ModelSpec]:
        data, _ = read_yaml((self.root / "settings.yaml").read_text(encoding="utf-8"))
        return read_models(data if isinstance(data, dict) else {})

    def _drawn(self) -> bool:
        """Whether the walk draws: each step's lobby apart and its summary on the main screen."""
        return bool(getattr(self.screen, "drawn", False))

    @contextmanager
    def _apart(self) -> Iterator[None]:
        apart = getattr(self.screen, "step_apart", None)
        if apart is None or not self._drawn():
            yield
            return
        with apart():
            yield

    def models(self) -> None:
        """Model selection: a row a job, Enter on one opens its provider, then that provider's models with their
        prices. Done in a picker goes back to the list with no change, and Escape steps back one picker. A pick is
        written to settings.yaml as it is made; Escape at a pick writes nothing."""
        status: list[str] = []
        cursor = 0
        mark = self.mark or (lambda m: m.symbol(False))
        with self._apart():
            while self._asking():
                specs = self._specs()
                got = self.screen.menu(models_menu(specs, status, mark), cursor)
                if got is None or got >= len(JOBS):
                    break
                cursor, job = got, JOBS[got]
                status = []
                picked = self._pick_model(job, specs[job])
                if picked is None:
                    continue
                self._write(job, lambda spec=picked, job=job: scaffold.set_model(self.root, job, spec))  # its row says it
        specs = self._specs()
        if self._drawn():
            from .output import Mark

            self.screen.styled("")  # type: ignore[attr-defined]
            self.screen.styled(self.screen.printer.bold("Model selection"))  # type: ignore[attr-defined]
            for job in JOBS:
                self.screen.printer.item(1, Mark.DONE, f"{ACTIVITIES[job][0]}: {specs[job].provider} "  # type: ignore[attr-defined]
                                                       f"{shown(specs[job].model, CAP)}")
            return
        self.screen.say("models    " + "; ".join(f"{job} {_named(spec)}" for job, spec in specs.items()))

    def _pick_model(self, job: str, current: ModelSpec) -> ModelSpec | None:
        """The provider, then one of its models: Done goes back to the list with no change, and
        Escape at a model goes back to the providers."""
        menu, providers = providers_menu(job)
        provider = current.provider
        while True:
            at = self.screen.menu(menu, next((i for i, p in enumerate(providers) if p == provider), 0))
            if at is None or at >= len(providers):
                return None
            provider = providers[at]
            models, choices = model_menu(provider, job)
            pick = self.screen.menu(models, next((i for i, c in enumerate(choices) if c.model == current.model), 0))
            if pick is None:
                continue
            if pick >= len(choices):
                return None
            chosen = choices[pick]
            return ModelSpec(chosen.provider, chosen.model,
                             current.effort if chosen.provider == current.provider else "", job)

    def _key_session(self) -> key_lobby.KeySession:
        """The keys step's session on this workspace: rows read afresh from the models and the store, each key proven
        by `prove`, else by the app's own proof call, before it is kept."""
        stored = [""]

        def read() -> list[key_lobby.KeyRow]:
            with self.open_workspace(self.root) as workspace:
                kept = workspace.secrets.names() if workspace.secrets is not None else []
                stored[0] = key_lobby.stored_where(workspace.secrets, self.system)
            return key_lobby.key_rows(self._specs(), kept, self.environ)

        def keep(provider: str, value: str) -> None:
            with self.open_workspace(self.root) as workspace:
                if workspace.secrets is None:
                    raise CerebroError("settings.yaml has no id, so no secrets store is open")
                workspace.secrets.set(provider, value)

        def prove(row: key_lobby.KeyRow, value: str) -> None:
            if self.prove is not None:
                self.prove(row, value)
                return
            job, spec = row.jobs[0]
            with self.open_workspace(self.root) as workspace:
                workspace.prove_key(job, spec, value)

        read()
        return key_lobby.KeySession(self.screen, read, prove, keep, stored[0], self.mark or (lambda m: m.symbol(False)))

    def keys(self) -> None:
        """The keys lobby, as `cerebro secrets` alone shows it: one row a provider the jobs use, each key typed hidden,
        proven with one small call and kept in the workspace's secrets store."""
        import click

        session = self._key_session()
        stopped = False
        if self._asking():
            try:
                with self._apart():
                    session.lobby(key_lobby.head())
            except KeyboardInterrupt:  # Ctrl-C between one prompt and the next: what stands is said, then 130
                self.screen.say("")
                stopped = True
        with key_lobby.held_ctrl_c():
            rows = session.read()
            if self._drawn():  # the summary: where the keys stand, and the command that manages them
                from .output import Mark

                got = sum(r.set for r in rows)
                said = ("No model keys set" if got == 0 else "Model key set" if len(rows) == 1 else
                        f"All {len(rows)} model keys set" if got == len(rows) else f"{got} of {len(rows)} model keys set")
                mark = Mark.FAILED if got == 0 else Mark.DONE if got == len(rows) else Mark.WARNING
                if rows:
                    self.screen.summary(mark, said,  # type: ignore[attr-defined]
                                        "Manage provider credentials with cerebro secrets set <provider>.")
                self.checks.extend(session.checks())
            else:
                said, _ = key_lobby.summary(rows)
                self.screen.say("keys      " + said)
                for line in session.checks():
                    self.screen.say("check     " + line)
                    self.checks.append(line)
        if stopped:
            raise click.exceptions.Exit(130)

    def sources(self) -> None:
        """connect's own lobby, on this workspace, then, where the walk draws, a summary of the sources connected."""
        if self._asking():
            self.lobby()
        if not self._drawn():
            return
        from .connect import places
        from .connect_screen import ATTENTION, CONNECTED, NO_LOGIN
        from .output import Mark

        with self.open_workspace(self.root) as workspace:
            rows = places(workspace)
        titles = [p.title or p.name for p in rows if p.state in (CONNECTED, NO_LOGIN)]
        attention = [p for p in rows if p.state == ATTENTION]
        needs = ", ".join(p.title or p.name for p in attention) + (" needs attention" if len(attention) == 1 else
                                                                   " need attention")
        reason = f"{attention[0].title or attention[0].name}: {attention[0].reason}" if attention else ""
        listed = f"Connected {len(titles)} source{'' if len(titles) == 1 else 's'}: {', '.join(titles)}"
        if not titles and not attention:
            self.screen.summary(Mark.FAILED, "No sources connected", "Connect sources with cerebro connect.")  # type: ignore[attr-defined]
        elif not titles:
            self.screen.summary(Mark.WARNING, needs, shown(reason, 300))  # type: ignore[attr-defined]
        elif attention:
            self.screen.summary(Mark.WARNING, f"{listed}; {needs}", shown(reason, 300))  # type: ignore[attr-defined]
        else:
            self.screen.summary(Mark.DONE, listed,  # type: ignore[attr-defined]
                                "Manage sources with cerebro connect, or in the sources/ folder of the workspace.")

    def _client_row(self, client: mcp_clients.Client, entry: mcp_clients.Entry) -> tuple[str, str, str]:
        """A client's row: installed when its config holds the server by name for this workspace,
        with the command it starts when that is not this one; another workspace's when the name starts
        another, which is not installed for this one; else not installed."""
        held = mcp_clients.found(client, entry.name)
        if held is None:
            return client.app, NOT_INSTALLED, ""
        by_hand = client.app == mcp_clients.CLAUDE_CODE  # claude refuses a name it holds: its steps change it by hand
        if mcp_clients.other_workspace(held, entry):
            args = [str(a) for a in held.get("args") or []]
            where = f"held for {args[-1] if args else mcp_clients._words(held)}"
            return client.app, NOT_INSTALLED, f"{where}: Enter shows the steps" if by_hand else where
        if held.get("command") != entry.command:
            return client.app, INSTALLED, f"starts {held.get('command')}" + (
                ": Enter shows the steps" if by_hand else ": Enter offers this command")
        return client.app, INSTALLED, ""

    def _writes(self, client: mcp_clients.Client) -> bool:
        return client.app != mcp_clients.CLAUDE_CODE or self.run_tool is not None

    def _choices(self, client: mcp_clients.Client, entry: mcp_clients.Entry) -> list[str]:
        """What can be done for a client: write a missing entry, update this workspace's to this command, or replace
        another workspace's, the person's say, each only where init writes that client; and always the steps."""
        held = mcp_clients.found(client, entry.name)
        if not self._writes(client) or (held is not None and client.app == mcp_clients.CLAUDE_CODE):
            return [SHOW]  # claude refuses a name it holds, so its entry is changed by hand
        if held is None:
            return [WRITE, SHOW]
        if mcp_clients.other_workspace(held, entry):
            return [REPLACE, SHOW]
        return [UPDATE, SHOW] if held.get("command") != entry.command else [SHOW]

    def clients(self) -> None:
        """The MCP clients lobby: each written only where the person says so, an entry another workspace holds
        replaced only on their say, and each write proven by reading its file back. A client init does not write
        opens its steps by hand alone."""
        from .output import Mark

        made = scaffold.mcp_entry(self.root, self.command)
        entry = mcp_clients.Entry(made["name"], made["command"], tuple(made["args"]))
        listed = mcp_clients.clients(self.home, self.system, self.environ)
        mark = self.mark or (lambda m: m.symbol(False))
        printer = getattr(self.screen, "printer", None) if self._drawn() else None
        status: list[str] = []
        cursor = 0
        written: list[str] = []
        with self._apart():
            while self._asking():
                rows = [self._client_row(c, entry) for c in listed]
                got = self.screen.menu(clients_menu(rows, status, mark), cursor)
                if got is None or got >= len(listed):
                    break
                cursor, client = got, listed[got]
                status = []
                choices = self._choices(client, entry)
                while True:
                    how = self.screen.menu(client_menu(client.app, choices)) if len(choices) > 1 else 0
                    if how is None or choices[how] != SHOW:
                        break
                    self.screen.read(manual_lines(client.app, mcp_clients.manual(client, entry, self.home), printer))
                    if len(choices) == 1:
                        break
                if how is None or choices[how] == SHOW:
                    continue
                try:
                    mcp_clients.write(client, entry, self.run_tool, replace=choices[how] == REPLACE)
                except mcp_clients.ClientError as error:
                    status = [f"{mark(Mark.FAILED)} {client.app} was not written: {' '.join(str(error).split())}"]
                    continue
                written.append(client.app)
                status = [f"{mark(Mark.DONE)} {client.app} connected ({entry.name})"]
        held = [c.app for c in listed if self._client_row(c, entry)[1] == INSTALLED]
        if self._drawn():  # a summary of the clients that hold the entry
            hint = "Manage MCP clients with cerebro init."
            if mcp_clients.CLAUDE_DESKTOP in written:
                hint = "Restart Claude Desktop to load it, and manage MCP clients with cerebro init."
            if held:
                n = len(held)
                self.screen.summary(Mark.DONE, f"{n} MCP client{'' if n == 1 else 's'} installed: "  # type: ignore[attr-defined]
                                               f"{', '.join(held)}", hint)
            else:
                self.screen.summary(Mark.FAILED, "No MCP clients installed",  # type: ignore[attr-defined]
                                    "Install MCP clients later with cerebro init.")
            return
        self.screen.say("mcp       " + (f"{', '.join(held)} {'holds' if len(held) == 1 else 'hold'} {entry.name}"
                                         if held else "no client set up for this workspace: cerebro init again "
                                                      "opens this step"))

    def _nothing_to_read(self) -> bool:
        """Whether a first run would read nothing: no source is connected, as
        connect's own lobby says it, and local/ holds no Markdown file at any depth, the files the local source
        reads, so a stray file such as .DS_Store counts for nothing."""
        from .connect import places
        from .connect_screen import CONNECTED

        with self.open_workspace(self.root) as workspace:
            if any(place.state == CONNECTED for place in places(workspace)):
                return False
        local = self.root / "local"
        return not (local.is_dir() and any(p.is_file() for p in local.rglob("*.md")))

    def _first_run_waits(self) -> bool:
        """Whether the first run has not run: no article of a curated collection,
        or of any collection when none is curated, is written yet."""
        with self.open_workspace(self.root) as workspace:
            curated = sorted({agent.target for agent in workspace.agents if agent.role == "curator"})
        folder = self.root / "articles"
        if not folder.is_dir():
            return True
        collections = curated or sorted(p.name for p in folder.iterdir() if p.is_dir())
        return not any(p.is_file() for c in collections if (folder / c).is_dir() for p in (folder / c).glob("*.md"))

    def _ready(self) -> None:
        """The Ready block, under the summaries: what the first run does. No question follows it: discovery starts,
        and the choice of what to build comes after it, on the run's own screen."""
        p = self.screen.printer  # type: ignore[attr-defined]
        self.screen.rows = 0  # type: ignore[attr-defined]
        for line in ("", "Ready to fetch from your sources and compile the first articles.", p.dim(READY)):
            self.screen.styled(line)  # type: ignore[attr-defined]

    def run(self) -> None:
        """The first run, with serve --once's own choice after discovery, started only when it can go: with
        every model key set and something to read. Otherwise the walk says why it waits. It starts with no question:
        what to build is chosen after discovery. Once an article is written the first run has run, and the walk says
        nothing of it."""
        if not self._first_run_waits():
            self.next = "cerebro serve"
            return
        missing = [r for r in self._key_session().read() if not r.set]
        drawn = self._drawn()
        if missing:
            them = "it" if len(missing) == 1 else "them"
            if drawn:  # a muted line under the summaries
                keys = _and([f"the {r.title} key for {r.job_words()}" for r in missing])
                self.screen.muted(f"The first run waits for {keys}: set {them} with cerebro secrets set, then run "  # type: ignore[attr-defined]
                                  "cerebro serve --once.")
            else:
                self.screen.say(f"next      the first run waits for {_and([f'the {r.title} key' for r in missing])}: "
                                f"set {them} with cerebro secrets set, then run cerebro serve --once")
            self.next = f"cerebro secrets set {missing[0].provider}"
            return
        if self._nothing_to_read():
            line = ("the first run would read nothing yet: local/ holds no Markdown file and no other source is "
                    "connected: connect one with cerebro connect, then run cerebro serve --once")
            if drawn:
                self.screen.muted("The first run would read nothing yet: local/ holds no Markdown file and no other "  # type: ignore[attr-defined]
                                  "source is connected. Connect one with cerebro connect, then run cerebro serve --once.")
            else:
                self.screen.say("next      " + line)
            self.checks.append(line)
            return
        if self._asking():  # discovery starts with no question: the choice of what to build comes after it
            if drawn:
                self._ready()
            self.first_run()
            # Done after discovery writes nothing: the first run waits, and serve --once comes back to it
            self.next = "cerebro serve --once" if self._first_run_waits() else "cerebro serve"
            return
        self.screen.say("next      cerebro serve --once runs the first pass")
        self.next = "cerebro serve --once"
