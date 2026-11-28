"""A pass's live progress on a terminal.

The region is drawn again whole, in place, on every event of the pass and as the spinner turns. It holds three groups
in a fixed order, Distilling, Compiling and Agents. Each group shows under its header once it has a row: the work that
ended, in the order it ended, then the work in flight with its spinner and its time. Under the groups come the line
of the sources being checked and a footer. The footer counts what is complete of the stage under way and what is in
flight, then the time since the pass started. A failure threads under the row of the work it is about, muted, wrapped
at its spaces. A distill pass under way counts the records it is done with of those it goes through. A line said to
the person goes under the groups. When the pass ends, the region is drawn a last time, whole, with nothing in flight,
and a blank line, so it stays above the summary.

A first run shows its discovery on live steps: `FirstRunScreen` reads the sources on a step whose
sub-lines count what each source listed, gives each curator a step of its own, queued, then reviewing,
then resolved to what it proposed or why it failed, records the approval on a step, and draws the build in the
region.

The screen gets the pass's events and nothing else: it never reads the workspace. Off a terminal, with `--plain` or
with `NO_COLOR`, it is not used at all: the pass prints its plain lines. Every word that came from a
source or a model goes through `shown`.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .output import SPIN_EVERY, Mark, Printer, Step, _climb, _wrap, cut, spinner, visible_width
from .shown import shown

GROUPS = (("distill", "Distilling"), ("compile", "Compiling"), ("agents", "Agents"))
CHECKING = "Checking     "  # the live line of the sources checked, padded so what follows lines up
MARKS = {"done": Mark.DONE, "failed": Mark.FAILED, "warning": Mark.WARNING, "skipped": Mark.QUIET, "ran": Mark.QUIET}
NAME_CAP = 120  # a key or a line's words past this many characters are cut, with a note of how long they were
THREAD = "    "  # what went wrong sits under its row's name, past its mark
THREAD_CAP = 3  # the most lines a row's thread shows, the rest counted in a line of their own


@dataclass
class _Row:
    """One piece of work: started, and once it ends, its mark, its words and what went wrong."""

    stage: str
    key: str
    started: float
    ended: float | None = None
    mark: str = ""
    text: str = ""
    why: str = ""
    done: int = 0  # a distill pass's records done, of `total`, and the facts it gave so far, while it runs
    total: int = 0
    facts: str = ""
    notes: list[str] = field(default_factory=list)  # what the pass warned of its work, threaded under it at its end
    cost: str = ""


def elapsed(seconds: float) -> str:
    """A row's time in whole seconds: 0s, 12s, 3m 12s or 1h 4m."""
    whole = int(max(seconds, 0.0))
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m {whole % 60}s"
    return f"{whole // 3600}h {whole // 60 % 60}m"


def minutes(seconds: float) -> str:
    """An agent's time in whole minutes and seconds, as the Agents block's column reads it: 0m 50s or 66m 40s."""
    whole = int(max(seconds, 0.0))
    return f"{whole // 60}m {whole % 60}s"


def _pad(text: str, width: int) -> str:
    return text + " " * (width - visible_width(text))


def thousands(n: int) -> str:
    """n with commas between groups of three digits, as 8,652."""
    return f"{n:,}"


class LiveProgress:
    """The region, fed one `Progress` event at a time from the pass's threads. `now` stands in for the clock,
    `height` for the terminal's rows, and `ticking=False` keeps the spinner still, as a test's frames are."""

    def __init__(self, printer: Printer, *, now: Callable[[], float] | None = None, limit: int = 8,
                 height: Callable[[], int] | None = None, ticking: bool = True, above: bool = False) -> None:
        self.p = printer
        self.above = above  # lines stand above the region, which a blank line parts it from
        self.now = now or time.monotonic
        self.limit = max(limit, 1)
        self._height = height
        self.ticking = ticking
        self.start = self.now()
        self.rows: list[_Row] = []  # in the order the work started
        self.order: list[_Row] = []  # the work that ended, in the order it ended
        self.checks: dict[str, float] = {}  # the sources being checked, and when each check started
        self.totals: dict[str, tuple[int, int]] = {}  # each stage's count as it opened, and its work complete by then
        self.counting = ""  # the stage the footer counts: the one opened last
        self.said: list[str] = []  # the lines said to the person amid the progress
        self.fresh: dict[str, int] = {}  # by stage, the work done with nothing moved, which shows no line
        self._lock = threading.RLock()
        self._drawn: list[int] = []
        self._drawn_at = 0  # the width the region was last drawn at
        self._open = False
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # -- the pass's events

    def __call__(self, event: Any) -> None:
        with self._lock:
            now = self.now()
            if event.stage == "check":
                if event.kind == "start":
                    self.checks[event.key] = now
                elif event.kind == "end":
                    began = self.checks.pop(event.key, now)
                    if event.mark == "failed":  # a source whose check failed has a row of its own under Distilling
                        row = _Row("distill", event.key, began, now, "failed", "check failed", event.why)
                        self.rows.append(row)
                        self.order.append(row)
            elif event.kind == "open":
                self.totals[event.stage] = (event.total, self._complete(event.stage))
                self.counting = event.stage
            elif event.kind == "start":
                self.rows.append(_Row(event.stage, event.key, now))
            elif event.kind == "count":  # how far a distill pass is through its records
                row = next((r for r in self.rows if r.stage == event.stage and r.key == event.key and r.ended is None),
                           None)
                if row is not None:
                    row.done, row.total, row.facts = event.done, event.total, event.text
            elif event.kind == "note":  # what grounding did, as counts, on the row of the work that ended
                row = next((r for r in reversed(self.order) if r.stage == event.stage and r.key == event.key), None)
                if row is not None:
                    row.notes.insert(0, event.text)
            elif event.kind == "end":
                row = next((r for r in self.rows if r.stage == event.stage and r.key == event.key and r.ended is None),
                           None)
                if row is None:
                    row = _Row(event.stage, event.key, now)
                    self.rows.append(row)
                row.ended, row.mark, row.text, row.why = now, event.mark, event.text, event.why
                row.cost = event.cost
                if event.mark == "fresh":  # nothing it reads moved: done, with no line
                    self.fresh[event.stage] = self.fresh.get(event.stage, 0) + 1
                else:
                    self.order.append(row)
            if event.kind != "count" or not self.ticking:  # a record's count shows as the clock turns: the
                self._redraw()                              # region draws on its tick, not on each record

    # -- the region

    def frame(self) -> list[str]:
        """The region as it stands, one string per line, styled as the printer styles."""
        with self._lock:
            return self._frame(self.now(), final=False)

    def _frame(self, now: float, final: bool) -> list[str]:
        width = self.p.width()
        out: list[str] = []
        for stage, header in GROUPS:
            done = [row for row in self.order if row.stage == stage]
            flying = [] if final else [row for row in self.rows if row.stage == stage and row.ended is None]
            if not done and not flying:
                continue
            if out:
                out.append("")
            out.append(self.p.label(header))
            columns = None
            if stage == "agents":  # a warning's long words run past the column, so the other rows keep their cost
                worded = [row for row in done if row.mark != "warning"]
                columns = (max(visible_width(shown(row.key, NAME_CAP)) for row in [*done, *flying[:self.limit]]),
                           max((visible_width(shown(row.text, NAME_CAP)) for row in worded), default=0),
                           max((visible_width(row.cost) for row in done), default=0))
            for row in done:
                out.append(self._ended(row, width) if columns is None else self._agent(row, width, columns))
                out += self._threads(row, width)
            for row in flying[:self.limit]:
                counts = (f"{self.p.dim(self._counts(row))}  " if row.total else "")
                name = self._name(row, flying=True)
                name = _pad(name, columns[0]) + "  " if columns is not None else name
                took = minutes(now - row.started) if columns is not None else elapsed(now - row.started)
                out.append(cut(f"  {self._spin(now - row.started)} {name}  {counts}{self.p.dim(took)}", width - 1))
            if len(flying) > self.limit:
                out.append(self.p.dim(f"  … and {len(flying) - self.limit} more"))
        for line in self.said:
            out += [""] if out else []
            out.append(line)
        if self.checks and not final:
            began = min(self.checks.values())
            names = ", ".join(shown(name, NAME_CAP) for name in self.checks)
            out += [""] if out else []
            out.append(cut(f"{self._spin(now - began)} {CHECKING}{names}   {self.p.dim(elapsed(now - began))}",
                           width - 1))
        elif not out and not self.counting:  # no count has opened: the sources are checked, or nothing is due
            out.append(f"{CHECKING}nothing to run" if final else
                       f"{self._spin(now - self.start)} {CHECKING}sources   {self.p.dim(elapsed(now - self.start))}")
        if self.counting:
            out += ["", *self._footer(now, final, width)]
        return out

    def _spin(self, took: float) -> str:
        return self.p.key(spinner(took))

    def _name(self, row: _Row, flying: bool) -> str:
        if row.stage == "compile" and not flying:  # an article's file name, then its path, which carries its collection
            return f"{shown(row.key.rsplit('/', 1)[-1], NAME_CAP)}  {self.p.dim(shown(f'articles/{row.key}.md', None))}"
        return shown(row.key, NAME_CAP)

    def _counts(self, row: _Row) -> str:
        """A distill pass's counts while it runs, in the words its row ends with: records done of those it goes
        through, then the facts it gave so far."""
        return f"{thousands(row.done)} of {thousands(row.total)} records • {shown(row.facts, NAME_CAP)}"

    def _threads(self, row: _Row, width: int) -> list[str]:
        """A row's thread: why it failed, then what the pass warned of its work, as many as THREAD_CAP, then how many
        more there are."""
        lines = [*([row.why] if row.why else []), *row.notes]
        out = [piece for line in lines[:THREAD_CAP] for piece in self._thread(line, width)]
        if len(lines) > THREAD_CAP:
            out.append(THREAD + self.p.dim(f"and {len(lines) - THREAD_CAP} more"))
        return out

    def thread(self, lines: list[str]) -> set[str]:
        """Thread each of `lines` the pass ended with that is about an article or an agent's run with a row here,
        `<key>: <what>` or `<agent> on <key>: <what>`, under that row, after why it failed, and give the lines that
        show within the row's cap, which need not be said again. One past the cap only counts in the row's line of
        the rest, and is said after the region, so a failure past the cap that no file keeps is still seen."""
        shown: set[str] = set()
        with self._lock:
            rows = [row for row in self.order if row.stage in ("compile", "agents")]
            for line in lines:
                for row in rows:
                    rest = _about(line, row.key)
                    if rest is None:
                        continue
                    if row.why.endswith(rest) or rest in row.text:  # said already, as why it failed or ran
                        shown.add(line)
                        break
                    row.notes.append(rest)
                    if len(row.notes) + bool(row.why) <= THREAD_CAP:
                        shown.add(line)
                    break
        return shown

    def _thread(self, why: str, width: int) -> list[str]:
        """What went wrong, muted under its row's name, wrapped at its spaces to the width."""
        if not why:
            return []
        room = max(width - 1 - len(THREAD), 1)
        return [THREAD + self.p.dim(piece) for piece in _words(shown(why, None), room, cut_long=True)]

    def _ended(self, row: _Row, width: int) -> str:
        mark = self.p.mark(MARKS.get(row.mark, Mark.DONE))
        text = shown(row.text, NAME_CAP)
        said = (f"  {self.p.dim(text)}" if row.stage == "compile" and row.mark in ("skipped", "ran") else
                f"  {self.p.dim(text)}" if text and row.stage != "compile" else "")
        took = self.p.dim(elapsed((row.ended or row.started) - row.started))
        return cut(f"  {mark} {self._name(row, flying=False)}{said}  {took}", width - 1)

    def _agent(self, row: _Row, width: int, columns: tuple[int, int, int]) -> str:
        """An agent's ended row in the block's columns: its name, what it did, what it spent and how long it took."""
        mark = self.p.mark(MARKS.get(row.mark, Mark.DONE))
        cells = [_pad(shown(row.key, NAME_CAP), columns[0])]
        cells += [self.p.dim(_pad(shown(row.text, NAME_CAP), columns[1]))] if columns[1] or row.text else []
        cells += [self.p.dim(_pad(row.cost, columns[2]))] if columns[2] else []
        took = minutes((row.ended or row.started) - row.started)
        return cut(f"  {mark} {'    '.join(cells)}    {self.p.dim(took)}", width - 1)

    def _complete(self, stage: str) -> int:
        return sum(1 for row in self.order if row.stage == stage) + self.fresh.get(stage, 0)

    def _footer(self, now: float, final: bool, width: int) -> list[str]:
        total, before = self.totals[self.counting]
        complete = self._complete(self.counting) - before  # what the stage did before it opened is not of its count
        flying = 0 if final else sum(1 for row in self.rows if row.ended is None) + len(self.checks)
        parts = [f"{thousands(complete)} of {thousands(total)} complete",
                 f"{flying} in flight", elapsed(now - self.start)]
        lines, line = [], ""
        for part in parts:  # the footer fits the width, or breaks between its parts and then at a word
            joined = f"{line} • {part}" if line else part
            if visible_width(joined) <= width - 1 or not line:
                line = joined
                continue
            lines.append(line)
            line = part
        lines.append(line)
        return [self.p.dim(piece) for line in lines for piece in _words(line, width - 1)]

    # -- drawing

    def say(self, line: str) -> None:
        """Say a line to the person amid the progress, such as a lock taken over: it goes under the
        groups, which go on growing above it."""
        with self._lock:
            self.said.append(shown(line, None))
            self._redraw()

    def begin(self) -> None:
        """Draw the region's first frame, which says the sources are checked, and start the spinner's clock. Lines
        above it stand apart from it by a blank line."""
        with self._lock:
            if self.above:
                self.p.out.write("\n")
            self._open = True
            self._redraw()
        if self.ticking:
            self._ticker = threading.Thread(target=self._tick, name="cerebro-progress", daemon=True)
            self._ticker.start()

    def finish(self) -> None:
        """Draw the region a last time, whole, with nothing in flight, and leave it above what prints next."""
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(1)
        with self._lock:
            if not self._open:
                return
            whole = self._frame(self.now(), final=True)
            self.p.out.write(self._unlive() + "\n".join(whole) + "\n\n")
            self.p._tail = "\n\n"  # it ends with a blank line, which what the printer says next does not double
            self.p.out.flush()
            self._open, self._drawn = False, []

    def _tick(self) -> None:
        while not self._stop.wait(SPIN_EVERY):
            try:
                with self._lock:
                    self._redraw()
            except Exception:  # noqa: BLE001 - a fault of the drawing stops the clock, never the run
                return

    def _rows(self) -> int:
        if self._height is not None:
            return self._height()
        try:
            return os.get_terminal_size(self.p.out.fileno()).lines or 24
        except (OSError, ValueError, AttributeError):
            return 24

    def _redraw(self) -> None:
        if not self._open:
            return
        rows = self._frame(self.now(), final=False)
        room = max(self._rows() - 1, 1)
        rows = rows[-room:]  # a region taller than the screen shows its tail: the footer and the rows that fit
        self.p.out.write(self._unlive() + "\n".join(rows))
        self.p.out.flush()
        self._drawn, self._drawn_at = [visible_width(row) for row in rows], self.p.width()

    def _unlive(self) -> str:
        if not self._drawn:
            return ""
        return "\r\x1b[K" + "\x1b[A\x1b[K" * max(_climb(self._drawn, self._drawn_at, self.p.width()) - 1, 0)


def _about(line: str, key: str) -> str | None:
    """What line says of the work `key` names, past `<key>: ` or `<agent> on <key>: `, or None when it is about other
    work."""
    if line.startswith(f"{key}: "):
        return line[len(key) + 2:]
    head, on, rest = line.partition(f" on {key}: ")
    if on and " " not in head:
        return rest
    head, on, rest = line.partition(": ")
    if on and head.startswith(f"{key} on ") and " " not in head[len(key) + 4:]:
        return rest
    return None


def _words(line: str, width: int, cut_long: bool = False) -> list[str]:
    """line broken at a word so each piece fits width columns; a single word wider than that stands alone, or with
    `cut_long` is cut to fit, with an ellipsis."""
    if visible_width(line) <= width:
        return [line]
    if cut_long:
        return [cut(piece, width) for piece in _words(line, width)]
    pieces, piece = [], ""
    for word in line.split(" "):
        joined = f"{piece} {word}" if piece else word
        if piece and visible_width(joined) > width:
            pieces.append(piece)
            piece = word
        else:
            piece = joined
    return [*pieces, piece]


# ---- a first run -----------------------------------------------------------------------------------------------------

# The lines of a first run's `say` the screen shows in their own place, on a step or in the region: what the pass and
# discovery say as each step happens. Any other line is said as it comes.
SHOWN_ON_STEPS = ("stage ", "listed ", "read ", "proposed ", "failed ", "stopped ", "ran ", "One more list: ")


class _Rows:
    """The stream the printer writes to, which counts the rows its cursor stands below where counting began: a newline
    goes down one, each cursor-up sequence up as many, as the printer's live lines move it. The printer fits and
    breaks every line it keeps, so no row wraps on its own."""

    def __init__(self, out: Any) -> None:
        self.out = out
        self.rows = 0

    def write(self, s: str) -> int:
        import re

        self.rows += s.count("\n")
        for up in re.findall(r"\x1b\[(\d*)A", s):
            self.rows -= int(up or 1)
        return self.out.write(s)

    def flush(self) -> None:
        self.out.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.out, name)


def _and(names: list[str]) -> str:
    """names joined in prose: a, b and c."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _records(n: int) -> str:
    return f"{thousands(n)} record{'' if n == 1 else 's'}"


def _sources(n: int) -> str:
    return f"{thousands(n)} source{'' if n == 1 else 's'}"


class FirstRunScreen:
    """A first run on a terminal, fed the first run's events.

    The sources are read on the step `Reading sources`, with a sub-line for each source in name order that counts
    what it listed, 0 until it lists, which resolves to `Read 239 records from 3 sources`, or with the warning mark
    and what failed. Discovery says `Running discovery for parts and things collections.` and gives each curator a
    step, `Queued for things collection` until it starts. A curator's step is then `Reviewing sources for things
    collection`, its spinner and its clock turning while it runs, resolved in the curators' order to `Proposed 4
    articles to things collection`, `Nothing new for things collection`, `Budget reached for things collection`
    or, with the failed mark, why it did not finish. The list is arranged on `Reviewing 12 proposals`, which resolves to
    `Reviewed 12 proposals`. An approval is recorded on `Recording 6 approvals`, which resolves to what it admitted.
    The build is the pass's region, the late curators' runs among its Agents. `sources` are the workspace's sources,
    each counted from the start."""

    def __init__(self, printer: Printer, *, sources: list[str] | tuple[str, ...] = (), limit: int = 8,
                 now: Callable[[], float] | None = None, height: Callable[[], int] | None = None,
                 ticking: bool = True, region: bool = False) -> None:
        self.p = printer
        # Inside init the run is a region of its own: the
        # first approval takes the discovery round and its list off the screen, so what it approved comes first. The
        # rows are counted as the printer leaves them; a list drawn and cleared by its own screen leaves none.
        self.rows: _Rows | None = None
        if region:
            self.rows = _Rows(printer.out)
            printer.out = self.rows  # type: ignore[assignment]
        self.sources = list(sources)
        self.limit = limit
        self._now, self._height, self._ticking = now, height, ticking
        self._lock = threading.RLock()
        self.reading: Step | None = None
        self.listed: dict[str, int] = {}  # what each source listed, as the read goes
        self.failed: list[str] = []  # each source whose check failed, with why
        self.read = 0  # the sources whose check ended done
        self.queued: list[str] = []  # the curators discovery asks, until each has its queued step
        self.asking = 0  # how many discovery asks
        self.collections: dict[str, str] = {}  # each curator's collection, by its id
        self.steps: dict[str, Step] = {}  # each curator's step, while it is live
        self.ended = False  # a curator's step resolved: what follows stands apart from them
        self.recording: Step | None = None
        self.arranging: Step | None = None
        self.region: LiveProgress | None = None
        self.building = False
        self.said: set[str] = set()  # the curators whose failure a step said
        self.lines_said: set[str] = set()  # and each such failure as the run's lines say it
        self.broken = ""  # why the live display stopped, after a fault of its own: the rest prints plain
        self.handled: set[str] = set()  # the warnings a build's own region took, and those it showed
        self.cue = 0  # the rows of the line that announced a late list, while it stands
        self.threaded: set[str] = set()

    # -- the first run's events

    def __call__(self, event: Any) -> None:
        """Take one event of the first run. A fault of the drawing never reaches the run: the live display stops,
        says so once, and the run's lines print plain from then on."""
        if self.broken:
            return
        try:
            self._take(event)
        except Exception as error:  # noqa: BLE001 - the run goes on whatever the screen does
            self._break(error)

    def _break(self, error: BaseException) -> None:
        """Stop the live display after a fault of its own: no step or region draws again, and one line says so."""
        with self._lock:
            if self.broken:
                return
            self.broken = f"{type(error).__name__}: {error}"
            for step in [*self.steps.values(), *(s for s in (self.reading, self.recording, self.arranging)
                                                 if s is not None)]:
                step._stop()
            if self.region is not None:
                self.region._stop.set()
                self.region._open = False
            self.p._steps, self.p._open = [], False
            self.steps, self.reading, self.recording, self.arranging, self.region = {}, None, None, None, None
        with contextlib.suppress(Exception):
            self.p.out.write(f"\nThe live display stopped ({shown(self.broken, NAME_CAP)}); the rest prints as plain "
                             "lines.\n")
            self.p.out.flush()

    def _take(self, event: Any) -> None:
        with self._lock:
            if event.stage == "build":
                self._build(event.kind == "start", event.why)
            elif self.building and event.stage in ("check", "distill", "compile", "agents"):
                self._region()(event)
            elif event.stage in ("read", "check"):
                self._read(event)
            elif event.stage == "discover":
                self._discover(event)
            elif event.stage == "approve":
                self._approve(event)
            elif event.stage == "arrange":
                self._arrange(event)
            elif event.stage == "late":
                self._late(event.total)

    def _read(self, event: Any) -> None:
        if event.stage == "read" and event.kind == "start":
            self.listed, self.failed, self.read = dict.fromkeys(self.sources, 0), [], 0
            self.reading = self.p.step("Reading sources")
            self._counts()
        elif event.stage == "check" and self.reading is not None:
            if event.kind == "start":
                self.listed.setdefault(event.key, 0)
            elif event.kind == "end":
                self.listed[event.key] = event.total
                if event.mark == "failed":
                    self.failed.append(f"{shown(event.key, NAME_CAP)}: {shown(event.why, NAME_CAP)}")
                else:
                    self.read += 1
            self._counts()
        elif event.stage == "read" and event.kind == "end" and self.reading is not None:
            step, self.reading = self.reading, None
            line = f"Read {_records(event.total)} from {_sources(self.read)}"
            if self.failed:
                step.collapse(Mark.WARNING, self._fit(f"{line}; {'; '.join(self.failed)}"))
            elif event.total > 0:
                step.collapse(Mark.DONE, self._fit(line))
            else:
                step.clear()

    def _counts(self) -> None:
        if self.reading is not None:
            self.reading.lines(*(f"{shown(name, NAME_CAP)}: {_records(n)}" for name, n in sorted(self.listed.items())))

    def _discover(self, event: Any) -> None:
        if event.kind == "open":
            self.queued, self.asking = [], event.total
        elif event.kind == "queue":
            self.collections[event.key] = event.text
            self.queued.append(event.key)
            if len(self.queued) == self.asking:  # discovery's line, then a queued step for each curator
                names = sorted(dict.fromkeys(shown(self.collections[id], NAME_CAP) for id in self.queued))
                self.p.line(_wrap(f"Running discovery for {_and(names)} collection{'' if len(names) == 1 else 's'}.", 0,
                                  self.p.width()))
                for id in self.queued:
                    self.steps[id] = self.p.step_along(f"Queued for {self._of(id)} collection")
        elif event.kind == "start":
            self.collections.setdefault(event.key, event.text)
            label = f"Reviewing sources for {self._of(event.key)} collection"
            if (step := self.steps.get(event.key)) is not None:
                step.relabel(label)
            else:
                self.steps[event.key] = self.p.step_along(label)
        elif event.kind == "end":
            step = self.steps.pop(event.key, None)
            if step is None:
                return
            if event.mark == "done":
                step.done(self._fit(f"Proposed {shown(event.text, NAME_CAP)}"))
            elif event.mark == "budget":
                step.fail(f"Budget reached for {self._of(event.key)} collection")
            elif event.mark == "failed":  # why sits under the line, muted and wrapped at its spaces
                room = max(self.p.width() - 1 - len(THREAD), 20)
                step.lines(*_words(shown(event.why, None), room))
                step.fail(self._fit(shown(event.text, NAME_CAP) or "Did not finish"))
                self.said.add(event.key)
                self.lines_said.add(f"{event.text}: {event.why}")
            else:  # names its collection: several curators resolve to it, and each must be told apart
                of = self._of(event.key)
                step.done(f"Nothing new for {of} collection" if of else "Nothing new")
            self.ended = True

    def _fit(self, text: str) -> str:
        """A step's resolved words, which the step breaks at a space where they would pass the terminal's width."""
        return text

    def _of(self, id: str) -> str:
        return shown(self.collections.get(id, ""), NAME_CAP)

    def _approve(self, event: Any) -> None:
        if event.kind == "start":
            self.settle()
            if not self._drop():  # the run's lines stay: a blank line parts the approval from them, one, never two
                self.p.line("")
            n = event.total
            self.recording = self.p.step("Recording your answer" if n == 0 else
                                         f"Recording {thousands(n)} approval{'' if n == 1 else 's'}")
        elif event.kind == "end" and self.recording is not None:
            step, self.recording = self.recording, None
            if event.mark != "done":  # nothing approved: the line that says so comes after
                step.clear()
                return
            step.lines(*(event.why.split("\n") if event.why else []))
            step.done(self._fit(shown(event.text, NAME_CAP)))

    def _arrange(self, event: Any) -> None:
        n = f"{thousands(event.total)} proposal{'' if event.total == 1 else 's'}"
        if event.kind == "start":
            self.arranging = self.p.step(f"Reviewing {n}")
        elif self.arranging is not None:
            step, self.arranging = self.arranging, None
            rows = step.done(f"Reviewed {n}")
            if self.cue:  # the late list's line and this one under it go with the list
                self.cue += rows

    def _build(self, start: bool, said: str = "") -> None:
        if start:
            self.settle()
            self.building = True
            self._region()
            return
        self.building = False  # the region stays, to take the pass's warnings under their rows as the run ends
        if said and self.region is not None:  # this build's own warnings go under its own rows, once: a later
            lines = said.split("\n")         # build's region, or the run's end, does not take them again
            self.threaded |= self.region.thread([line for line in lines if line not in self.handled])
            self.handled.update(lines)

    def _late(self, curators: int) -> None:
        """Announce a late list, under the build's region drawn a last time, on a line the list's approval takes
        away with the list."""
        from .first_run import late_line

        self.settle()
        text = _wrap(late_line(curators), 0, self.p.width())
        self.p.line(text)
        self.cue = _climb([visible_width(line) for line in text.split("\n")], 0, self.p.width())

    def _drop(self) -> bool:
        """Take the run's region off the screen, so what was approved comes next, on a terminal where its first row is
        still on screen, and say a blank line in its place, under what stands above it. Once only: the late list's
        approval finds no region. Whether it erased it."""
        if self.cue:  # the line that announced the late list goes with the list, which cleared itself
            self.p.out.write(f"\r\x1b[{self.cue}A\x1b[J")
            self.p.out.flush()
            self.cue, self.p._tail = 0, "\n\n"
            return True
        rows, self.rows = self.rows, None
        if rows is None:
            return False
        self.p.out = rows.out  # type: ignore[assignment]
        if rows.rows <= 0 or rows.rows >= self.p.height():
            return False
        self.p.out.write(f"\r\x1b[{rows.rows}A\x1b[J\n")
        self.p.out.flush()
        self.p._tail = "\n\n"
        return True

    def list_shown(self, drawn: bool) -> None:
        """A list was shown: `drawn` when its own screen drew it and cleared it, leaving the screen as it was; else
        its lines were printed past the printer, so the region cannot be counted, and the printer's blank line is
        said afresh."""
        if not drawn:
            if self.rows is not None:
                self.p.out = self.rows.out  # type: ignore[assignment]
            self.rows, self.cue = None, 0  # a printed list stays, and the line above it with it
            self.p._tail = ""

    def _region(self) -> LiveProgress:
        """The build's region, opened as its first event comes, or again after a question took the screen."""
        if self.region is None:
            self.region = LiveProgress(self.p, now=self._now, limit=self.limit, height=self._height,
                                       ticking=self._ticking, above=True)
            self.region.begin()
        return self.region

    def _close(self) -> None:
        if self.region is not None:
            self.region.finish()
            self.region = None

    # -- the lines said, and questions

    def say(self, line: str) -> None:
        """A line the first run says: one a step or the region shows in its place goes there; any other is said
        now, in the region while the build runs, or above the live steps."""
        if self.broken:  # the live display stopped: every line prints plain, as it lands
            print(line, file=self.p.out, flush=True)
            return
        if line.startswith(SHOWN_ON_STEPS):
            return
        try:
            with self._lock:
                if self.region is not None:
                    self.region.say(line)
                else:
                    self.p.line(line)
        except Exception as error:  # noqa: BLE001 - the run goes on whatever the screen does
            self._break(error)
            print(line, file=self.p.out, flush=True)

    def settle(self) -> None:
        if self.broken:
            return
        try:
            self._settle()
        except Exception as error:  # noqa: BLE001 - the run goes on whatever the screen does
            self._break(error)

    def _settle(self) -> None:
        """Leave the screen to a question, the list or a line of its own: each step still live goes, with no line,
        and the region is drawn a last time. What comes next stands apart from the curators' steps by a blank line."""
        with self._lock:
            for step in [*self.steps.values(), *(s for s in (self.reading, self.recording, self.arranging)
                                                 if s is not None)]:
                step.clear()
            self.steps, self.reading, self.recording, self.arranging = {}, None, None, None
            self._close()
            if self.ended:
                self.p.line("")
                self.ended = False

    def finish(self) -> None:
        """End the first run's screen: whatever is still live goes, as `settle` leaves it."""
        self.settle()
        self.building = False

    def shown_keys(self) -> set[str]:
        """The work whose failure the screen said on its own line: each curator a step said failed."""
        return set(self.said)

    def close(self, lines: list[str]) -> set[str]:
        """End the screen as the run ends, `lines` the pass's warnings, threaded under their rows in the region's
        last frame; give those the screen showed, which need not be said again."""
        try:
            with self._lock:
                new = [line for line in lines if line not in self.handled]
                threaded = self.region.thread(new) if self.region is not None else set()
            self.finish()
            return threaded | self.threaded
        except Exception as error:  # noqa: BLE001 - the run goes on whatever the screen does
            self._break(error)
            return set()
