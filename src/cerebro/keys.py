"""The model keys a workspace's jobs need, as the keys lobby shows them: `init`'s walk and `cerebro secrets` alone.

One row a model provider the jobs use, in job order, extraction, compilation and decision, a provider that serves two
jobs on one row, with whether its key is set and the jobs it serves. Extraction also needs the facts layer's own
decision model, so its provider serves extraction too. A row is `connected` when the secrets store keeps its key,
`in the environment` when only the provider's variable gives one, which the app reads after the store, and `not added`
with neither.

A key typed in is proven before it is kept: one small call on the row's first job's model, through `prove`, which the
caller gives, so a test stands in for it. The app's `Cerebro.prove_key` is the real one, checked against the day's
budget before it starts, as every call is. A key is never shown, logged or put in a line: each line is masked of the
value before it is said.
"""

from __future__ import annotations

import contextlib
import re
import signal
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .facts import FACTOID_DECISION
from .models import PROVIDERS, ModelSpec

if TYPE_CHECKING:
    from .output import Mark

__all__ = ["CONNECTED", "IN_ENVIRONMENT", "LOBBY_JOBS", "NOT_ADDED", "PROOF", "KeyRow", "KeySession", "head",
           "key_rows", "menu", "stored_where", "summary", "table"]

LOBBY_JOBS = ("extraction", "compilation", "decision")  # the order the lobby lists providers in
CONNECTED, IN_ENVIRONMENT, NOT_ADDED = "connected", "in the environment", "not added"
PROOF = "key_proof"  # what a key's proof call is recorded under in the day's spend
KEYS_PLACE = "API keys settings"  # where a console makes keys, as a person reads it
ESC = "\x1b"  # the Escape key, as a hidden line reads it


RUN = 6  # the fewest characters of a key in a row that a line may never hold


def unkeyed(text: str, value: str) -> str:
    """`text` with the key `value` taken out, whole as `****`, and any run of RUN characters or more of it, as a
    reply cut short leaves, as `****` too: so no line kept or shown holds part of the key."""
    text = text.replace(value, "****") if value else text
    for start in range(len(value) - RUN + 1):
        while (piece := value[start:start + RUN]) in text:
            at = text.index(piece)
            end = at + RUN
            while end < len(text) and end - at < len(value) - start and text[end] == value[start + end - at]:
                end += 1
            text = text[:at] + "****" + text[end:]
    return text


@dataclass(frozen=True)
class KeyRow:
    """One row of the keys lobby: a provider, the jobs its key serves with the model each runs, in lobby order, and
    where its key stands. Never the key."""

    provider: str
    jobs: tuple[tuple[str, ModelSpec], ...]
    state: str

    @property
    def title(self) -> str:
        return PROVIDERS[self.provider].label

    @property
    def set(self) -> bool:
        return self.state != NOT_ADDED

    def job_words(self) -> str:
        """The row's jobs as a person reads them, as extraction and decision."""
        names = list(dict.fromkeys(job for job, _ in self.jobs))
        return names[0] if len(names) < 2 else ", ".join(names[:-1]) + " and " + names[-1]

    def mark(self) -> Mark:
        from .output import Mark

        return Mark.FAILED if self.state == NOT_ADDED else Mark.DONE


def key_rows(models: Mapping[str, ModelSpec], kept: Iterable[str], environ: Mapping[str, str]) -> list[KeyRow]:
    """The lobby's rows for `models`: one a provider they give a job, in job order, a provider serving two jobs on
    one row, its state from the names the store keeps and the provider's variable in `environ`."""
    pairs: list[tuple[str, ModelSpec]] = []
    for job in LOBBY_JOBS:
        if (spec := models.get(job)) is not None:
            pairs.append((job, spec))
        if job == "extraction":  # the facts layer decides with its own model while it extracts
            pairs.append(("extraction", FACTOID_DECISION))
    held = set(kept)
    grouped: dict[str, list[tuple[str, ModelSpec]]] = {}
    for job, spec in pairs:
        if spec.provider in PROVIDERS:
            grouped.setdefault(spec.provider, []).append((job, spec))
    return [KeyRow(provider, tuple(jobs), CONNECTED if provider in held else IN_ENVIRONMENT
                   if environ.get(PROVIDERS[provider].variable, "").strip() else NOT_ADDED)
            for provider, jobs in grouped.items()]


def table(rows: Sequence[KeyRow], mark: Callable[[Mark], str] = lambda m: m.symbol(False)) -> list[str]:
    """The rows as a table: each provider, its mark and where its key stands, padded so
    the words line up, and the jobs it serves."""
    first = max((len(r.title) for r in rows), default=0) + 2
    marks = [mark(r.mark()) for r in rows]
    width = max((len(r.mark().symbol(False)) for r in rows), default=0)
    second = width + 1 + max((len(r.state) for r in rows), default=0) + 2
    out = []
    for r, m in zip(rows, marks, strict=True):
        state = m + " " * (width - len(r.mark().symbol(False))) + " " + r.state
        visible = width + 1 + len(r.state)
        out.append((r.title.ljust(first) + state + " " * (second - visible) + r.job_words()).rstrip())
    return out


def summary(rows: Sequence[KeyRow]) -> tuple[str, str]:
    """How many of the keys are set, and the command to run next: `secrets set` for the
    first one missing, or `status` once every key is set."""
    got = sum(r.set for r in rows)
    missing = next((r for r in rows if not r.set), None)
    said = ("Model key set" if got else "No model keys set") if len(rows) == 1 else f"{got} of {len(rows)} model keys set"
    return said, f"cerebro secrets set {missing.provider}" if missing else "cerebro status"


# A key's escape sequence as a terminal sends it: CSI, as an arrow, Delete or a paste marker, or SS3, as an F-key.
_KEY_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1bO[ -~]")


# Ctrl-\, Ctrl-Z and Ctrl-Y, which a view takes as keys where the terminal would stop or end the command with them.
_VIEW_KEYS = str.maketrans("", "", "\x1c\x1a\x19")


def typed(value: str) -> tuple[str, bool]:
    """A line typed at a hidden prompt as the key it holds, with each key's escape sequence in it left out, so an
    arrow is ignored, and so are Ctrl-\\, Ctrl-Z and Ctrl-Y, which a view reads and does nothing with;
    and whether Escape alone was pressed, which cancels."""
    value = _KEY_SEQUENCE.sub("", value).translate(_VIEW_KEYS)
    return value, ESC in value


def _interrupted(error: BaseException) -> bool:
    """Whether Ctrl-C stands behind `error`: an interrupt another error took the place of as it was cleaned up."""
    seen = error.__context__
    while seen is not None:
        if isinstance(seen, KeyboardInterrupt):
            return True
        seen = seen.__context__
    return False


@contextlib.contextmanager
def held_ctrl_c() -> Iterator[None]:
    """Ctrl-C held while the block runs, never raised in it, and let go after: a key its provider accepted is kept
    and said whole."""
    try:
        old = signal.signal(signal.SIGINT, lambda signum, frame: None)
    except ValueError:  # not the main thread, where no signal comes
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old)


def control(value: str) -> bool:
    """Whether `value` holds a control character, which no key holds: Escape, a paste marker, a byte typed by
    mistake."""
    import unicodedata

    return any(unicodedata.category(char) == "Cc" for char in value)


@dataclass
class KeySession:
    """One run of the keys step, the lobby's or the plain line's: the rows read afresh each time, what was kept, and
    why each key that did not verify did not, by provider, until one does.

    `screen` asks and says, as `guided.Screen`; `read` gives the rows as the settings and the store stand now; `prove`
    makes the proof call on a row's first job, raising with why it did not verify; `keep` stores a key under its
    provider; `stored` says where a pasted key is kept; `mark` draws a mark for the menu's stream."""

    screen: Any
    read: Callable[[], list[KeyRow]]
    prove: Callable[[KeyRow, str], None]
    keep: Callable[[str, str], None]
    stored: str
    mark: Callable[[Mark], str] = lambda m: m.symbol(False)
    added: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def open(self, row: KeyRow, label: str = "", skips: bool = False, drawn: bool = False,
             say: Callable[[str], None] | None = None) -> str:
        """The provider's view: where its key is made and where it is kept, then the key at a hidden prompt, proven
        on the first job the models, read again once it is typed, give the provider, and kept. The line that says how
        it went, or "" when nothing was typed. `drawn`, in the lobby, styles the view:
        the provider's name as a region heading, the two lines as notes with the keys place in the accent, and a blank
        line before the prompt; on plain lines it is the three lines as they are. `say`, on plain lines,
        says the line once it is made, so a key kept is said before anything a Ctrl-C held while it was kept does."""
        from .output import Mark

        def said(line: str) -> str:
            if say is not None and line:
                say(line)
            return line

        title = row.title
        view(self.screen, title, self.stored, drawn)
        while True:
            line = self.screen.text(label or f"{title} API key", hidden=True)
            if line is None:  # input that ends goes back with nothing stored or proven
                return ""
            value, escaped = typed(line)
            if escaped:  # Escape alone goes back too; an arrow or another key's sequence in a key is left out
                return ""
            if value.strip() or skips:
                break  # Enter with nothing typed is ignored in the view; on a plain line it skips
        if not value.strip():
            return ""
        value = value.strip()
        if control(value):  # a key never holds one: it is not proven, and not kept
            return said(f"{self.mark(Mark.FAILED)} {title} key holds a control character, so it was not kept: type "
                        "or paste the key alone")
        current = next((r for r in self.read() if r.provider == row.provider), None)
        if current is None:
            return said(f"{self.mark(Mark.WARNING)} {title} is no longer named in settings.yaml: the key was not "
                        "stored")
        step = self.screen.step(f"Verifying {title} connection") if drawn and hasattr(self.screen, "step") else None
        cancelled = "cancelled by Ctrl-C"
        with contextlib.ExitStack() as late:
            try:
                try:
                    self.prove(current, value)
                    late.enter_context(held_ctrl_c())  # from here a Ctrl-C is too late to stop a key its provider accepted
                    why = ""
                except KeyboardInterrupt:  # Ctrl-C while the key is proven: nothing of it is kept
                    why = cancelled
                except Exception as error:  # noqa: BLE001 - every failed proof becomes one line, the key masked
                    why = cancelled if _interrupted(error) else (
                        " ".join(unkeyed(str(error), value).split()) or type(error).__name__)
                if why == cancelled and not skips:
                    import click

                    raise click.exceptions.Exit(130) from None  # the drawn lobby ends
                if why:  # on plain lines a cancelled proof fails as any other, and the next key is asked
                    self.failed[row.provider] = why
                    if step is not None:
                        step.fail(f"{title} did not verify: {why}")
                    return said(f"{self.mark(Mark.FAILED)} {title} did not verify: {why}")
                try:
                    self.keep(row.provider, value)
                except BaseException:
                    if step is not None:
                        step.fail("the key was not stored")
                    raise
                if step is not None:
                    step.done(f"{title} connection verified, key added to secrets store")
            finally:
                if step is not None:
                    step.clear()  # a step not ended above, as by Ctrl-C or a signal, leaves no line
            self.failed.pop(row.provider, None)
            if row.provider not in self.added:
                self.added.append(row.provider)
            return said(f"{self.mark(Mark.DONE)} {title} connection verified, key added to secrets store")

    def lobby(self, head: Sequence[str]) -> None:
        """The menu of providers, opened one at a time until Done or Escape, the cursor kept on the row opened and a
        line under the intro that says how its key went."""
        status, cursor = "", 0
        while True:
            rows = self.read()
            got = self.screen.menu(menu(rows, head, status, self.mark), cursor)
            if got is None:
                return
            cursor = got
            apart = getattr(self.screen, "apart", None)
            if apart is None:
                status = self.open(rows[got], drawn=True) or status
                continue
            with apart():  # the view is gone once it ends, so the lobby redraws alone
                status = self.open(rows[got], drawn=True) or status

    def plain(self, echo: Callable[[str], None]) -> None:
        """At a terminal that draws nothing: the rows as a table, then the names to set, with commas, by id or title,
        and each named key asked for in turn at a hidden prompt, where Enter skips it."""
        from .guided import ask

        rows = self.read()
        for line in table(rows):
            echo(line)

        def names(answer: str) -> list[KeyRow]:
            from .errors import CerebroError

            picked: list[KeyRow] = []
            for part in (p.strip() for p in answer.split(",")):
                if not part:
                    continue
                row = next((r for r in rows if part.casefold() in (r.provider, r.title.casefold())), None)
                if row is None:
                    raise CerebroError(f"{part} is not one of {', '.join(r.provider for r in rows)}")
                if row not in picked:
                    picked.append(row)
            return picked

        answer = ask(self.screen, "Set which? Names, with commas", names)
        for row in names(answer or ""):
            echo("")
            self.open(row, f"{row.title} API key [Enter to skip]", skips=True, say=echo)

    def checks(self) -> list[str]:
        """One line for each provider still with no key: why its last key did not verify, or that it has none yet,
        with the command that sets it."""
        out = []
        for r in self.read():
            if r.set:
                continue
            command = f"cerebro secrets set {r.provider}"
            why = self.failed.get(r.provider)
            out.append(f"the {r.title} key did not work for {r.job_words()} ({why}): run {command}" if why
                       else f"no {r.title} key yet for {r.job_words()}: run {command}")
        return out


def menu(rows: Sequence[KeyRow], head: Sequence[str], status: str = "",
         mark: Callable[[Mark], str] = lambda m: m.symbol(False)) -> Any:
    """The lobby's menu: each provider, its mark and where its key stands, and the jobs it serves; never a key. Enter
    selects one, and Done, the last row, or Escape leaves."""
    from .checklist import MenuList, MenuRow

    return MenuList(rows=[MenuRow(r.title, r.state, r.job_words(), mark=mark(r.mark())) for r in rows],
                    head=[*head, *([status] if status else [])], end="Done", open="select")


def view(screen: Any, title: str, stored: str, drawn: bool = False) -> None:
    """A provider's view before its prompt: its name, where its key is made and where it is kept. Drawn, on a screen
    that styles: the name as a region heading, the two lines as notes with the keys place
    in the accent, and a blank line; else the three lines as they are."""
    place = f"Make a key in the {KEYS_PLACE} of the {title} console and paste it below."
    if drawn and hasattr(screen, "note"):
        screen.heading(title)
        screen.note(place, accent=KEYS_PLACE)
        screen.note(stored)
        screen.note("")
        return
    for line in (title, place, stored):
        screen.say(line)


def stored_where(store: Any, system: str) -> str:
    """The line a provider's view says where a pasted key is kept in, by the kind of store: the
    keychain, or the file only your user can read."""
    kind = type(store).__name__
    if kind == "KeychainSecrets":
        return "Pasted contents are not shown and are stored in your " + (
            "macOS Keychain." if system == "darwin" else "system keychain.")
    if kind == "FileSecrets":
        return "Pasted contents are not shown and are stored in a file only your user can read."
    return "Pasted contents are not shown and are stored in this workspace's secrets store."


def head(settings: str = "settings.yaml") -> list[str]:
    """The lines above the lobby's rows: its title, and an intro of one line, which the menu
    wraps at the terminal's width. `settings` is the file's name as drawn."""
    intro = ("Add keys for the following model providers to continue. Model selection can also be configured by "
             f"editing {settings} in your workspace and running cerebro secrets to add the required provider "
             "credentials.")
    return ["Model keys", intro]