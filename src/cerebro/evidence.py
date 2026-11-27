"""Evidence: what code gathers for an agent from its file's evidence lines, before the model starts.

Each line is one entry, read through `Lookup` as that agent. `query` and `facts` are queries, `facts` of the article
the run writes. `articles` reads articles by path, a collection by its folder, or the article `this` or a field of
it names, each with its facts, or as its `summary` or `text_only`. `left` gives the articles a query
returned at the agent's last kept write and returns no more. `files` reads whole files of the workspace, `script`
what a program in its folder prints, `sql` the rows of one read-only query of a database source, whose address and
login live in the source's file and the secrets store, never in the agent file. `held` lists the records held for a
curator. An app adds a kind with `@evidence("name")`. A window word such as `last_14_days` goes to the lookup as
written: the lookup resolves windows. A line that cannot be read as written stops the gather with one line that
names it.

An agent never reads text it wrote: a query returns front matter, never an article's text, an article comes
without the parts its reader wrote, and the article the run writes without those of any writer of the run.

Preload loads the entries in the order the file lists them, facts newest first, each item once, in the first entry
that names it, and stops at the first item that does not fit in half the run's token budget. A first entry then
says what was left out, as counts, with the paths and records to look it up by.

`script` runs its file, found by its real path inside the workspace's folder, through the process starter the caller
hands in, with an environment of `PATH` and the workspace's folder alone, a time limit and a size limit.

The digest is one hash of everything the lines returned, before the cut, so the runner can tell when an agent is
due. What the agent's own runs wrote about themselves counts nothing in it, though the agent reads it: its rows of
the logs, the fields it set on an article, and the parts rendered in its name. So a run of its own never makes it
due, and another agent's run, proposal or article does. A left entry counts nothing either: its query's own change
is what wakes the agent.

`keep` on an articles line cuts its articles into passages, has a judge score each against the line's instruction,
and loads only those that reach the bar.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from . import prompts
from .agent import Agent, Budget, EvidenceLine
from .apps import bind
from .article import Article, Citation, cut, fields
from .article import passages as passages_of
from .budget import BudgetSpent, Ledger
from .caller import Caller
from .clock import Clock, seconds, system_clock
from .connector import NOBODY, Connector, Record, one_line
from .connectors.database import Rows, row_text
from .errors import CerebroError
from .facts import FIELD_KIND, Fact, superseded
from .file import File
from .identity import follow, held_by, redirects, tied
from .lookup import Lookup, QueryResult
from .models import Decider, DecisionError, DecisionTooLarge, ModelSpec, TokenCount, YesNo, wire_id
from .records import WAITING, proposal_from
from .rendered import MARK
from .shown import shown
from .store import LOGS, MENTIONS, SUBJECT, Store, compares, own_key, update_state
from .window import Span, WindowError, holds, is_window, read_time, span
from .workspace import Workspace

__all__ = ["BUILTIN", "KEEP_BAR", "KEEP_BARS", "LEFT_OUT", "RUN_SPEND", "DecisionKeepJudge", "Evidence",
           "EvidenceError", "Finished", "Item", "KeepJudge", "KeepScores", "LastSets", "Launch", "NotBuilt", "Passage",
           "ScriptError", "SqlError", "Starter", "blocks", "choose", "digest", "estimate_tokens", "fingerprint", "gather",
           "gather_failed", "keep_bar", "left_key", "preload", "render", "start"]

BUILTIN = ("query", "facts", "articles", "left", "files", "sql", "script")
CITE = re.compile(r"\[\^([^\]\s]+)\]")


class EvidenceError(CerebroError, ValueError):
    """An evidence line cannot be gathered as written."""


class NotBuilt(CerebroError, NotImplementedError):
    """An evidence line its gather cannot run, since its caller handed in nothing the kind needs. The message names
    the file, the line, and what to pass."""


class ScriptError(EvidenceError):
    """A script line's script is refused by the rules, could not start, failed, or ran past its time limit."""


class SqlError(EvidenceError):
    """A sql line's query is refused as no read, failed, or ran past its time, or its source is missing or holds no
    database."""


@dataclass
class Item:
    """One thing a line returned: a fact, an article, a match, a record, or a file. `key` names it, so it loads once.
    `text` is how the run reads it, `at` orders facts newest first, and `left` is how a line that names it as left
    out spells it: an article's path, or the record a fact quotes. `counts` is what the digest hashes in its place,
    when part of it is what the reading agent's own runs wrote, as on the article it writes."""

    key: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    at: str = ""
    left: str = ""
    counts: str | None = None


@dataclass
class Evidence:
    """One gathered entry: a heading, the lines that loaded, what they let the run cite, and any files.

    `items` is everything the line returned, before preload cut it to the budget, and is what the digest hashes.
    `spelled` is the line as its file writes it. `left_out` is the line that says what of this entry did not load,
    and `notes` what the run should know of how it read. `returns_now` is None but on a left entry, where it holds
    the ids its query returns now, which compile keeps for the next run once this run's write lands. `counted` is
    what the digest hashes in place of `items` on a log's entry: the rows no run of the reading agent wrote.
    """

    heading: str
    lines: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    files: list[File] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    spelled: str = ""
    left_out: str = ""
    notes: list[str] = field(default_factory=list)
    returns_now: list[str] | None = None
    counted: list[Item] | None = None


@dataclass
class LastSets:
    """What compile kept at the agent's last kept write on one article: for each left line, by `left_key`, the
    article ids its query returned then, in that order, and `at`, the time that write's run read them. With no `at`
    known, each query is read as of now."""

    sets: dict[str, list[str]] = field(default_factory=dict)
    at: datetime | None = None


# -- gather: each evidence line, read as the agent

def gather(agent: Agent, lookup: Lookup, functions: dict[str, Callable[..., Any]], workspace: Workspace | None = None, *,
           this: str | None = None, writers: list[str] | None = None, root: Path | None = None,
           now: datetime | None = None, last: LastSets | None = None, starter: Starter | None = None,
           zone: tzinfo | None = None, fiscal_start: int | None = None,
           sources: Mapping[str, Connector] | None = None, judge: KeepJudge | None = None) -> list[Evidence]:
    """Each of the agent's evidence lines, gathered in the order the file lists them, then cut to half its budget.

    `this` is the article the run writes; an agent of one article writes its own path, and any other agent needs it
    named. `writers` are the agents whose text the run rewrites on that article, such as a composer and its
    subagents. `root` is the workspace's folder, which a `files` or `script` line reads. `now` is when the run reads,
    which a windowed query's heading names. `last` is what compile kept at the agent's last kept write on this
    article, which a `left` line compares with, and `starter` starts a `script` line's script: pass `start` for the
    real one. `zone` and `fiscal_start` are how a left line reads its query's windows as of then, the lookup's own
    when not given. `sources` are the workspace's connectors by name, which a `sql` line reads its database from;
    with none, such a line says the workspace has no such source. `judge` scores the passages of an articles line
    with `keep`; with none, such a line loads as it would without keep, and says why.
    """
    if this is None and (agent.role == "writer" or (agent.role == "composer" and "/" in agent.target)):
        this = agent.target
    gatherer = _Gatherer(agent, lookup, this, writers or [], root, now, last, starter, zone, fiscal_start, judge)
    out = []
    for n, line in enumerate(agent.evidence, 1):
        if line.kind in functions:
            out.append(gatherer.function(line.kind, bind(functions[line.kind], gatherer.caller, workspace), line))
        elif line.kind == "query":
            if not isinstance(line.args, dict) or not line.args.get("from"):
                raise gatherer.fail(n, line, "it names no from: write - query: { from: notes }")
            filters = {k: v for k, v in line.args.items() if k != "from"}
            out.append(gatherer.query(n, line, line.args["from"], filters, left_key(line)))
        elif line.kind == "facts":
            out.append(gatherer.facts(n, line))
        elif line.kind == "articles":
            out.append(gatherer.articles(n, line))
        elif line.kind == "files":
            out.append(gatherer.files(n, line))
        elif line.kind == "left":
            out.append(gatherer.left(n, line, {item.key for entry in out for item in entry.items}))
        elif line.kind == "script":
            out.append(gatherer.script(n, line))
        elif line.kind == "sql":
            out.append(gatherer.sql(n, line, sources or {}))
        elif line.kind == "held":  # the records held for a curator's collection
            out.append(_held(agent, lookup, sources or {}, n, line, root))
        else:
            raise EvidenceError(f"{gatherer.name}: evidence kind {line.kind!r} is not one Cerebro or an app gives: "
                                f"use one of {', '.join(BUILTIN)}, or an @evidence name")
    return preload(out, agent.budget)


RECENT_DAYS = 14  # a first run and its build read the records of these last days before the first run began
IN_FIRST_BUILD: ContextVar[bool] = ContextVar("in_first_build", default=False)  # while a first run's build runs


def _held(agent: Agent, lookup: Lookup, sources: Mapping[str, Connector], n: int, line: EvidenceLine,
          root: Path | None) -> Evidence:
    """A held line's entry, with who is on the workspace's own side as the workspace at `root` says, or no one. While
    the workspace's first run is open, and while its build runs, it lists the records of the last `RECENT_DAYS` days
    before the first run began, and counts the older ones: the runs after it list them."""
    from .held import before_cut, entry, first_run_open, first_run_since  # held imports this module: it loads here
    from .project import read_ours
    ours = read_ours(root) if root is not None else NOBODY
    store = getattr(lookup, "store", None)
    out = entry(agent, store, list(sources), n, line, ours)
    if not (first_run_open(store) or IN_FIRST_BUILD.get()):
        return out
    since = first_run_since(store)
    kept = [item for item in out.items if not before_cut(item.at, since)]
    if older := len(out.items) - len(kept):
        out.items = kept
        out.notes.append(f"{older} held records from before {since} are not listed on the first run: the runs after "
                         "it list them")
    return out


def _ordered(filters: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """A query line's filters without its `order_by:`, and the field it names, which orders it newest first."""
    order_by = filters.get("order_by")
    return {k: v for k, v in filters.items() if k != "order_by"}, None if order_by is None else str(order_by)


def left_key(line: EvidenceLine) -> str:
    """A query line as evidence spells it, such as `query from things, due this_quarter`, and so the key compile
    keeps a left line's set under."""
    filters = {k: v for k, v in line.args.items() if k != "from"}
    return f"query from {line.args['from']}" + (f", {_describe(filters)}" if filters else "")


# -- preload, render and the digest

LEFT_OUT = "What was left out"
INTRO = ("Your evidence loads in the order your agent file lists it, facts newest first, and stops at half this run's "
         "token budget. This was left out:")
HINT = ("When a claim needs something left out, look it up narrowly: read an article by its path, or query with a filter, "
        "such as the facts of one record.")
NONE = f"None of this entry loaded: see {LEFT_OUT} above."
GATHER = "gather: "  # how the note of a run that failed at gather begins


def estimate_tokens(text: str) -> int:
    """What the run budget counts text as: its UTF-8 bytes divided by 2.5, rounded up."""
    return (2 * len(text.encode("utf-8")) + 4) // 5


def preload(entries: list[Evidence], budget: Budget) -> list[Evidence]:
    """Load each entry's items in order until half the run's token budget is spent, and, when anything was left
    out, put first an entry that says what, with paths and records that fill at most a tenth of that half."""
    cap = budget.tokens_per_run // 2
    left, counted = _load(entries, cap)
    if not any(left):
        for entry in entries:
            entry.lines += entry.notes
        return entries
    # Something is cut, so load again under a cap that leaves room for the entry that says what.
    reserve = estimate_tokens(f"## {LEFT_OUT}\n\n{INTRO}\n\n{HINT}") + cap // 10
    reserve += sum(estimate_tokens(f"- entry {n}, {e.spelled or e.heading}: {t} of {t} not loaded\n{NONE}\n")
                   for n, (e, t) in enumerate(zip(entries, counted), 1))
    left, counted = _load(entries, max(0, cap - reserve))
    room, lines = cap // 10, [INTRO, ""]
    for n, (entry, out, total) in enumerate(zip(entries, left, counted), 1):
        if not out:
            continue
        entry.left_out = f"entry {n}, {entry.spelled or entry.heading}: {len(out)} of {total} not loaded"
        if not entry.lines:
            entry.lines.append(NONE)
        named = list(dict.fromkeys(item.left for item in out if item.left))
        shown = []
        for name in named:
            cost = estimate_tokens(f", {name}")
            if cost > room:
                break
            room -= cost
            shown.append(name)
        line = entry.left_out
        if shown:
            sep = ", from " if all(item.key.startswith("fact:") for item in out) else ": "
            line += sep + ", ".join(shown) + (f", and {len(named) - len(shown)} more" if len(shown) < len(named)
                                              else "")
        lines.append(f"- {line}")
    lines += ["", HINT]
    for entry in entries:
        entry.lines += entry.notes
    return [Evidence(LEFT_OUT, lines), *entries]


def _load(entries: list[Evidence], cap: int) -> tuple[list[list[Item]], list[int]]:
    """One pass of preload under `cap` tokens. Gives each entry's items left out, and how many items it counts:
    those no earlier entry named."""
    used, stopped, seen, left, counted = 0, False, set(), [], []
    for entry in entries:
        entry.lines, entry.citations, out, count = [], [], [], 0
        used += estimate_tokens(f"## {entry.heading}\n\n" + "\n".join(entry.notes))
        for item in entry.items:
            if item.key in seen:
                continue  # an earlier entry named it, and counts it
            seen.add(item.key)
            count += 1
            cost = estimate_tokens(item.text + "\n")
            if not stopped and used + cost <= cap:
                used += cost
                entry.lines.append(item.text)
                entry.citations += item.citations
            else:
                stopped = True
                out.append(item)
        left.append(out)
        counted.append(count)
    return left, counted


def render(evidence: list[Evidence]) -> str:
    """The evidence as the model reads it: each entry's heading, then its lines."""
    parts = []
    for entry in evidence:
        body = "\n".join(entry.lines) if entry.lines else "Nothing matched."
        parts.append(f"## {entry.heading}\n\n{body}")
    return "\n\n".join(parts)


def gather_failed(row: dict[str, Any]) -> bool:
    """Whether an `agent-runs` row is a run that failed at gather, before any model was asked."""
    return row.get("status") == "failed" and not row.get("run") and str(row.get("note") or "").startswith(GATHER)


def digest(agent: Agent, evidence: list[Evidence]) -> str:
    """One stable hash of the agent's file and of everything its lines returned, as `fingerprint` counts them."""
    return digest_of(fingerprint(agent, evidence))


def digest_of(printed: dict[str, str]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(printed, sort_keys=True).encode()).hexdigest()[:16]


def fingerprint(agent: Agent, evidence: list[Evidence]) -> dict[str, str]:
    """The agent's file and everything its lines returned, before preload cut it, item by item, so what moved can be
    told from what did not: the file under "", then each entry under its spelling, each item it counts under the entry
    and the item's key, and an entry with no items by its notes and its files. The budget, the day, and what was
    left out move nothing, and a new or changed fact, article, match or script output moves it. A left entry moves
    nothing: the change of its query is what wakes the agent. What the agent's own runs wrote moves nothing either: an
    entry's `counted` rows and an item's `counts` stand in for it."""
    def hashed(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:16]

    out = {"": hashed(agent.path.read_bytes() if agent.path.is_file() else agent.prompt.encode())}
    for entry in evidence:
        if (entry.heading == LEFT_OUT and not entry.items) or entry.returns_now is not None:
            continue
        name, files = entry.spelled or entry.heading, [f.data if f.data is not None else _file_bytes(f.path)
                                                       for f in entry.files]
        if entry.items or entry.counted is not None:
            out[name] = hashed(b"".join(files))
            for i in entry.items if entry.counted is None else entry.counted:
                out[f"{name}\0{i.key}"] = hashed((i.text if i.counts is None else i.counts).encode())
        else:
            out[name] = hashed("\n".join(entry.lines).encode() + b"".join(files))
    return out


def _file_bytes(path: Path | None) -> bytes:
    """A file's bytes for the digest, or its path when it cannot be read, so the run's own read says why."""
    try:
        return path.read_bytes() if path is not None else b""
    except OSError:
        return str(path).encode()


# -- how a fact, an article and a record read

UNSTAMPED = ("compiled_at", "refresh_at", "admitted")  # front matter every compile moves, which wakes no reader
TITLE_CAP = 120  # the characters of a title a fact's label shows


def _item(thing: Any) -> Item:
    if isinstance(thing, Fact):
        from .ground import quote_for  # grounding imports this module, so its helper loads at the call
        quote = quote_for(thing)
        text = f"- [^{thing.id}] {thing.text}" + (f' (quote: "{quote}", {thing.record})' if quote else "")
        if thing.status == "superseded":  # history only: compile keeps it out of any claim of what holds now
            text += f" [{superseded(thing)}]"
        return Item(f"fact:{thing.id}", text, [Citation("fact", thing.id, thing.record, quote)], thing.at,
                    thing.record)
    if isinstance(thing, Article):
        id = thing.identity.id
        text = f"- [^{id}] {thing.identity.title}: {json.dumps(_front(thing), sort_keys=True, default=str)}"
        return Item(f"article:{id}", text, [Citation("article", id, id)], left=f"articles/{id}.md")
    if isinstance(thing, Record):
        text = f"- [^{thing.id}] {thing.body or json.dumps(thing.fields, sort_keys=True, default=str)}"
        return Item(f"record:{thing.id}", text, [Citation("record", thing.id, thing.source)], thing.at, thing.id)
    if isinstance(thing, dict):
        key = f"row:{thing['seq']}" if "seq" in thing else "row:" + json.dumps(thing, sort_keys=True, default=str)
        return Item(key, f"- {json.dumps(thing, default=str)}")
    return Item(f"value:{thing!r}", f"- {thing}")


def _front(article: Article) -> dict[str, Any]:
    """An article's fields as a run reads them, but those every compile moves: the system's value of each protected
    field, never a writer's value of it, as `article.fields` gives them."""
    return {name: prop.value for name, prop in fields(article).items() if name not in UNSTAMPED}


def _front_text(article: Article) -> str:
    """An article's front matter as a kept article loads it and the judge reads it: its title, the identity's, and
    every field a run reads."""
    front = _front(article)
    if article.identity.title:
        front = {"title": article.identity.title, **front}
    return json.dumps(front, sort_keys=True, default=str)


def _article_text(label: str, front: dict[str, Any], parts: list[Any]) -> str:
    """An article as a run reads it: `label`, its front matter, then each part under its heading, a whole part cut at
    its own headings."""
    pieces = [(h, t) for s in parts for h, t in (cut(s.text) if s.whole else [(s.heading, s.text.strip())])]
    return "\n\n".join([label, json.dumps(front, sort_keys=True, default=str)]
                       + [f"#### {h}\n\n{t}" if h else t for h, t in pieces])


def _cited(article: Article, compilations: list[Any]) -> list[Citation]:
    """What a loaded article lets the run cite: the article, and each fact or record its text cites that the part
    keeps as a citation, with its quote. An id that only appears in the text cites nothing: a run citing it would be
    checked against the article's words, not against the fact."""
    cited = [Citation("article", article.identity.id, article.identity.id)]
    for compilation in compilations:
        known = {c.id: c for c in compilation.citations}
        cited += [known[id] for id in dict.fromkeys(CITE.findall(compilation.text)) if id in known]
    return cited


def _quoted(title: str) -> str:
    return json.dumps(shown(title, TITLE_CAP), ensure_ascii=False)


def _marked(item: Item, mark: str | None) -> Item:
    """A fact's item with the line an expire of its document puts under it, which the digest reads too, so a
    decision reruns the agents whose evidence holds the document's facts."""
    return item if not mark else replace(item, text=f"{item.text}\n  {mark}")


def _describe(args: dict[str, Any]) -> str:
    return ", ".join(f"{k} {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in args.items())


def _id(value: str) -> str:
    return value.strip().removeprefix("articles/").removesuffix(".md")


def _date(moment: datetime) -> str:
    return f"{moment:%b} {moment.day}, {moment.year}"


def _day(moment: datetime) -> str:
    return f"{moment:%a} {_date(moment)}"


# -- keep: the passages of an entry a judge scores against its instruction

# The score a passage needs for an entry with keep to load it, by decision model, unless the line sets its own with
# `keep_bar`. The score is the model's probability of yes, and each model answers on its own scale, so each bar
# below was measured on that model.
KEEP_BARS = {("typesafe", "jev-1.13.0"): 0.4, ("fireworks", "accounts/fireworks/routers/rune"): 0.1}
KEEP_BAR = 0.5  # the bar for a decision model nobody has measured
KEEP_PER_CALL = 40  # the most passages one decision scores
KEEP_PASSAGE_BYTES = 4000  # what of one passage the judge reads
KEEP_CACHE_DAYS = 30  # how long a score is kept from the day it was scored
KEEP_CACHE_ENTRIES = 10_000  # the most scores one instruction keeps, the oldest scored dropped first
KEEP_PROMPT = "keep-judge"  # the bundled prompt the judge reads, which a workspace may eject to prompts/keep-judge.md
KEEP_QUESTION = "Does passage {id} hold what the instruction asks for?"
KEEP_LINE = ("Each article below loads with its front matter and only the passages kept for this entry's instruction. The "
             "rest was left out on purpose: write from what is here rather than read the articles again.")


@dataclass
class Passage:
    """One piece of an entry's evidence that `keep` scores: an article's front matter as one passage, or one block of
    one of its parts, under the article's id, `article`, and the part's heading, `heading`. `score` is the judge's, from
    0 to 1, or None when it gave none; `kept` and `why` are what `choose` made of it, as `rank 3`, `below the bar of
    0.5` or `past top 60`."""

    article: str
    heading: str
    text: str
    front: bool = False
    score: float | None = None
    kept: bool = False
    why: str = ""

    def hash(self) -> str:
        """The passage's identity among scores: what the judge reads of it."""
        return hashlib.sha256(f"{self.article}\0{self.heading}\0{self.text}".encode()).hexdigest()[:24]


@dataclass
class KeepScores:
    """What a judge answered for a list of passages: a score from 0 to 1 for each, in order, or None for one it left
    unscored, with `wait` saying why; how many the decision model scored now and how many came from the cache."""

    scores: list[float | None]
    wait: str = ""
    scored: int = 0
    cached: int = 0


def keep_bar(decision: ModelSpec | None) -> float:
    """The score a passage needs to load on the decision model `decision`: its measured bar, or `KEEP_BAR`."""
    if decision is None:
        return KEEP_BAR
    return KEEP_BARS.get((decision.provider, wire_id(decision.provider, decision.model)), KEEP_BAR)


class KeepJudge:
    """The seam `keep` scores passages through: how well each meets the entry's instruction, from 0 to 1. `bar` is
    the score a passage needs on this judge's model, unless the line sets its own."""

    bar: float = KEEP_BAR

    def score(self, instruction: str, passages: list[Passage], bar: float = KEEP_BAR) -> KeepScores:
        """A score for each passage. `bar` is the score the entry keeps a passage at, which a judge may use to
        check a passage that falls under it."""
        raise NotImplementedError


class DecisionKeepJudge(KeepJudge):
    """The keep judge on the decision model: many passages to a call, a yes or no for each, its probability of yes
    the passage's score. A passage two entries share is asked about once. Its `bar` is the one `keep_bar` gives its
    decision model, and its words are the bundled prompt `keep-judge`, or the workspace's own copy at
    `prompts/keep-judge.md` under `root`. `calls` and `usage` total what it spent. With a `ledger`, each call is
    checked against the compilation cap before it starts and recorded after it, priced on `decision`: with the cap
    spent, it scores nothing, and the entry loads whole with the note that says why.

    With a `store`, each score is cached in its state by the decision model, the instruction and the judge's words,
    and by the passage's article, heading and text. So a passage whose text has not moved is not scored again for
    `KEEP_CACHE_DAYS` from when it was scored, however often it is read, and a new decision model, instruction or
    prompt scores every passage afresh. One instruction keeps at most `KEEP_CACHE_ENTRIES` scores, the oldest scored
    dropped first, so a large workspace cannot grow it without limit. Scores that compiles side by side cache are
    merged, so none is lost.

    One score is noisy: the same passage can score high in one call and near zero in another, as the batch around it
    changes. So with `again`, the default, a passage scored now under the bar, or left unscored, is asked once more,
    in a call with only those passages, in the other order, and keeps the higher of its two scores before it is
    cached. A score from the cache is not asked again, and no score is cached for a passage left unscored."""

    def __init__(self, decider: Decider, store: Store | None = None, clock: Clock = system_clock,
                 ledger: Ledger | None = None, decision: ModelSpec | None = None, root: Path | None = None,
                 again: bool = True):
        self.again = again
        self.decider, self.store, self.clock = decider, store, clock
        self.ledger, self.decision, self.root = ledger, decision, root
        self.calls, self.usage = 0, TokenCount()
        self.bar = keep_bar(decision)
        self._lock = threading.Lock()

    def score(self, instruction: str, passages: list[Passage], bar: float | None = None) -> KeepScores:
        bar = self.bar if bar is None else bar
        out = KeepScores([None] * len(passages))
        if not passages:
            return out
        contract = prompts.load(KEEP_PROMPT, self.root)
        model = f"{self.decision.provider}\0{self.decision.model}" if self.decision is not None else ""
        key = own_key("keep", "") + hashlib.sha256(
            f"{model}\0{contract}\0{instruction.strip()}".encode()).hexdigest()[:16]
        cache: dict[str, list[Any]] = dict(self.store.state(key) or {}) if self.store is not None else {}
        today = self.clock().astimezone(UTC).date()
        oldest = (today - timedelta(days=KEEP_CACHE_DAYS)).isoformat()
        asked: dict[str, list[int]] = {}
        moved = False
        for i, passage in enumerate(passages):
            hit = cache.get(passage.hash())
            if hit is not None and hit[1] >= oldest:  # the day it was scored: reading it does not renew it
                out.scores[i], out.cached = float(hit[0]), out.cached + 1
            else:
                asked.setdefault(passage.hash(), []).append(i)
        got = self._round(contract, instruction, passages, asked, list(asked), out)
        if self.again:  # once more for each under the bar, among other passages: the higher of the two stands
            low = [h for h in reversed(list(asked)) if got.get(h) is None or got[h] < bar]
            for h, score in self._round(contract, instruction, passages, asked, low, out).items():
                if score is not None:
                    got[h] = max(got[h] or 0.0, score)
        for h, score in got.items():
            if score is None:
                continue
            for i in asked[h]:
                out.scores[i] = score
            out.scored += len(asked[h])
            cache[h], moved = [score, today.isoformat()], True
        if self.store is not None and moved:
            fresh = {h: cache[h] for h, score in got.items() if score is not None}

            def merged(now: Any) -> dict[str, Any]:  # what a compile beside this one kept meanwhile stays
                both = {**dict(now or {}), **fresh}
                kept = sorted(((h, v) for h, v in both.items() if v[1] >= oldest), key=lambda hv: hv[1][1])
                return dict(kept[-KEEP_CACHE_ENTRIES:])

            update_state(self.store, key, merged)
        return out

    def _round(self, contract: str, instruction: str, passages: list[Passage], asked: dict[str, list[int]],
               hashes: list[str], out: KeepScores) -> dict[str, float | None]:
        """One round of asking: `hashes`, `KEEP_PER_CALL` to a call, each with its score, or None unscored."""
        got: dict[str, float | None] = {}
        for start in range(0, len(hashes), KEEP_PER_CALL):
            chunk = hashes[start:start + KEEP_PER_CALL]
            got.update(zip(chunk, self._ask(contract, instruction, [passages[asked[h][0]] for h in chunk], out)))
        return got

    def _ask(self, contract: str, instruction: str, batch: list[Passage], out: KeepScores) -> list[float | None]:
        """Scores for one batch, split in halves while a decision is too large. A decision that fails leaves its
        passages unscored, and `out.wait` says why."""
        ids = [f"p{n}" for n in range(1, len(batch) + 1)]
        scrub = (self.store.scrubbing() if self.store is not None else None) or (lambda text: text)
        state = {"contract": contract, "instruction": instruction.strip(), "passages": [  # masked before the cut
            {"id": id, "article": p.article, "heading": p.heading,
             "text": scrub(p.text).encode()[:KEEP_PASSAGE_BYTES].decode("utf-8", "ignore")} for id, p in zip(ids, batch)]}
        try:
            decision = self._decide(state, {id: YesNo(KEEP_QUESTION.format(id=id)) for id in ids})
        except BudgetSpent as error:
            out.wait = str(error)
            return [None] * len(batch)
        except DecisionTooLarge as error:
            if len(batch) > 1:
                half = len(batch) // 2
                return (self._ask(contract, instruction, batch[:half], out)
                        + self._ask(contract, instruction, batch[half:], out))
            out.wait = str(error)
            return [None]
        except DecisionError as error:
            out.wait = str(error)
            return [None] * len(batch)
        scores: list[float | None] = []
        for id in ids:
            answer = decision.answers.get(id)
            yes = answer.yes if answer is not None else None
            if yes is None or not 0 <= yes <= 1:
                out.wait = f"the decision model left the passage {id} unscored"
                scores.append(None)
            else:
                scores.append(float(yes))
        return scores

    def _decide(self, state: dict[str, Any], questions: dict[str, YesNo]) -> Any:
        """One call, checked against the compilation cap before it starts, as compile's held decider is, and
        recorded after it."""
        if self.ledger is not None:
            self.ledger.check("compilation", self.clock(), self.decision)
        decision = self.decider.decide(state, questions)
        with self._lock:
            u = decision.usage
            self.calls += 1
            self.usage = TokenCount(self.usage.input_tokens + u.input_tokens,
                                    self.usage.output_tokens + u.output_tokens,
                                    self.usage.cache_read_tokens + u.cache_read_tokens,
                                    self.usage.cache_write_tokens + u.cache_write_tokens)
        if self.ledger is not None and self.decision is not None:
            self.ledger.record("compilation", self.clock(), self.decision, decision.usage, "keep judge")
        return decision


def blocks(body: str) -> list[str]:
    """A part's text cut into passages: each paragraph, each item of a list with the lines under it, and each fenced
    block whole. A short line alone that only labels what follows, such as **Stuck** or `In flight:`, goes with the
    passage after it, and an HTML comment on its own line is no passage."""
    out: list[str] = []
    current: list[str] = []
    label = ""

    def flush() -> None:
        nonlocal label
        text = "\n".join(current).strip()
        current.clear()
        if not text:
            return
        if _label(text):
            label = f"{label}\n{text}".strip()
            return
        if label:
            text, label = f"{label}\n{text}", ""
        out.append(text)

    fence = ""
    for line in body.split("\n"):
        trimmed = line.strip()
        if fence:
            current.append(line)
            if trimmed.startswith(fence):
                fence = ""
                flush()
            continue
        if trimmed.startswith(("```", "~~~")):
            flush()
            fence = trimmed[:3]
            current.append(line)
            continue
        if not trimmed:
            flush()
            continue
        if trimmed.startswith("<!--") and trimmed.endswith("-->"):
            continue
        if trimmed.startswith("#"):
            flush()
            current.append(line)
            flush()
            continue
        if _list_item(line) and current:
            flush()
        current.append(line)
    flush()
    if label:
        out.append(label)
    return out


def _list_item(line: str) -> bool:
    """Whether a line opens an item of a list at the left margin: -, * or + and a space, or a number, a period or a
    parenthesis and a space."""
    return bool(re.match(r"[-*+] |\d+[.)] .", line))


def _label(text: str) -> bool:
    """Whether a passage is one short line that only labels what follows: a heading, a line in bold alone, or one
    that ends with a colon."""
    if "\n" in text or len(text) > 80:
        return False
    return text.startswith("#") or text.endswith(":") or (text.startswith("**") and text.endswith("**")) or (
        text.startswith("__") and text.endswith("__"))


def choose(passages: list[Passage], top: int | None, bar: float = KEEP_BAR) -> None:
    """Keep the passages that score `bar` or more, and of those, with `top`, the best `top` alone: best first, ties
    in the order they came. Each passage's `why` says where it fell. An article's front matter counts as one of its
    passages."""
    order = sorted(range(len(passages)), key=lambda i: -(passages[i].score or 0.0))
    n = 0
    for i in order:
        passage = passages[i]
        if passage.score is None:
            passage.kept, passage.why = False, "left unscored, so under the bar"
        elif passage.score < bar:
            passage.kept, passage.why = False, f"below the bar of {bar}"
        elif top is not None and n >= top:
            passage.kept, passage.why = False, f"past top {top}"
        else:
            n += 1
            passage.kept, passage.why = True, f"rank {n}"


def _kept_article(article: Article, kept: list[tuple[Passage, Any]], total: int) -> Item:
    """An article cut by keep: its front matter, then its kept passages, each under its heading, those of one part
    joined. `kept` pairs each passage with the part it came from, or None for the front matter."""
    id = article.identity.id
    held: list[Any] = []
    came: list[Any] = []
    for passage, source in kept:
        if source is None:
            continue  # the front matter loads first in any case
        if came and came[-1] is source:
            held[-1] = replace(held[-1], text=f"{held[-1].text}\n\n{passage.text}")
        else:
            held.append(replace(source, text=passage.text))
            came.append(source)
    parts = [f"### Article articles/{id}.md, {len(kept)} of its {total} passages, kept for this entry's instruction",
             _front_text(article)] + [f"#### {s.heading}\n\n{s.text}" for s in held]
    return Item(f"kept:{id}", "\n\n".join(parts), _cited(article, held), left=f"articles/{id}.md")


# -- script: a program in the workspace's folder, and the process starter that runs it

SCRIPT_SECONDS = 60.0  # how long a script may run before it is stopped
SCRIPT_BYTES = 1 << 20  # how much of what it prints is read; the rest is not, and the script is stopped
STDERR_BYTES = 4096  # how much of what it writes to stderr is kept, for the line that says why it failed
VENDORED = ("node_modules", "vendor", "site-packages", "__pypackages__", "__pycache__")  # never a script's folder
WORKSPACE_ENV = "CEREBRO_WORKSPACE"  # the one variable beside PATH a script gets: the workspace's folder


@dataclass
class Launch:
    """One script to start: the program and its arguments, the folder it runs in, its whole environment, and its
    limits. A starter adds nothing to the environment."""

    argv: list[str]
    cwd: Path
    env: dict[str, str]
    seconds: float = SCRIPT_SECONDS
    limit: int = SCRIPT_BYTES


@dataclass
class Finished:
    """How a script ended: its exit code, or None when it was stopped; the first `limit` bytes it printed, and of
    stderr; `cut` when it printed more than that and was stopped; and `timed_out` when it ran past its time."""

    code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    cut: bool = False
    timed_out: bool = False


Starter = Callable[[Launch], Finished]


def _decoded(data: bytes, cut: bool) -> str | None:
    """What a script printed as text, or None when it is not UTF-8. Output cut at the limit may end inside a
    character, which is dropped."""
    for trim in range(4 if cut else 1):
        try:
            return data[:len(data) - trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


# The keeper every script runs under on Linux: it makes itself the subreaper of all it starts, so a descendant that
# leaves the script's group or session is still its child once orphaned, and when the script exits, or on SIGTERM
# from `start`, it kills and reaps every descendant before it exits. Its exit is the script's own.
_KEEPER = r"""
import ctypes, os, signal, sys
ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
told = int(sys.argv[1])
os.set_inheritable(told, False)
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})  # a stop that lands before its handler waits for it
pid = os.fork()
if pid == 0:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    os.setpgid(0, 0)
    try:
        os.execvp(sys.argv[2], sys.argv[2:])
    except OSError as error:
        os.write(told, str(error.errno or 0).encode())
    os._exit(127)
os.close(told)

def kids():
    me, out = str(os.getpid()).encode(), []
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    if f.read().rsplit(b")", 1)[1].split()[1] == me:
                        out.append(int(name))
            except (OSError, IndexError):
                pass
    return out

def sweep():
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    while True:
        for kid in kids():
            try:
                os.kill(kid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return

def stopped(*_):
    sweep()
    os._exit(137)

signal.signal(signal.SIGTERM, stopped)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
_, status = os.waitpid(pid, 0)
sweep()
if os.WIFSIGNALED(status):
    sig = os.WTERMSIG(status)
    try:
        signal.signal(sig, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(os.getpid(), sig)
    os._exit(128 + sig)
os._exit(os.waitstatus_to_exitcode(status))
"""
KEEPER_WAIT = 2.0  # seconds the keeper is given to sweep and exit once told to, before its group is killed
DRAIN_WAIT = 0.25  # seconds output is still read once the script has ended, before the pipes are closed


def _kill_session(session: int) -> None:
    """Kill every process of `session` on Linux, read from /proc, until none is left or a few rounds have passed."""
    for _ in range(20):
        found = []
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open(f"/proc/{name}/stat", "rb") as f:
                        fields = f.read().rsplit(b")", 1)[1].split()
                    if fields[0] != b"Z" and int(fields[3]) == session:  # a dead one waits only to be reaped
                        found.append(int(name))
                except (OSError, IndexError, ValueError):
                    pass
        if not found:
            return
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        threading.Event().wait(0.01)


def start(launch: Launch) -> Finished:
    """The real process starter: run `launch.argv` in `launch.cwd` with exactly `launch.env`, no stdin, in a session
    of its own, stopped when it exits, runs past its time, or prints past its limit. Raises OSError if it can't start.

    On Linux the script runs under the keeper, the subreaper of everything it starts: a child that starts its own
    session or group, or whose parent exited, is still the keeper's to find, and the keeper kills and reaps every
    descendant before `start` returns, so nothing the script starts survives it, unless it kills the keeper first.
    On macOS and other systems the script's process group is killed, so a child that stays in it ends with the
    script, and a child that starts its own session or group can outlive it. Either way the pipes are closed a
    quarter second after it ends, so a surviving child cannot hold the gather open."""
    keeper = sys.platform == "linux"
    proc = _spawn(launch, keeper)
    assert proc.stdout is not None and proc.stderr is not None
    out, err = bytearray(), bytearray()
    limits = {proc.stdout.fileno(): (out, launch.limit), proc.stderr.fileno(): (err, STDERR_BYTES)}
    chooser = selectors.DefaultSelector()
    for fd in limits:
        os.set_blocking(fd, False)
        chooser.register(fd, selectors.EVENT_READ)
    deadline = seconds() + launch.seconds
    ended: float | None = None
    timed_out = cut = False
    try:
        while chooser.get_map():
            now = seconds()
            if ended is None and proc.poll() is not None:
                ended = now
                _stop(proc, keeper)  # what it left in its group ends with it
            if ended is None and now >= deadline:
                timed_out = True
                _stop(proc, keeper)
                ended = seconds()
            if ended is not None and seconds() >= ended + DRAIN_WAIT:
                break  # what still holds a pipe open is not waited for
            wait = (ended + DRAIN_WAIT if ended is not None else min(deadline, now + 0.05)) - seconds()
            for key, _ in chooser.select(max(wait, 0)):
                fd = key.fd
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    chooser.unregister(fd)
                    continue
                into, limit = limits[fd]
                if len(into) <= limit:
                    into.extend(chunk[:limit + 1 - len(into)])
                if fd == proc.stdout.fileno() and len(into) > limit and not cut:
                    cut = True
                    _stop(proc, keeper)
                    ended = seconds()
    finally:
        chooser.close()
        proc.stdout.close()
        proc.stderr.close()
    _stop(proc, keeper)  # what the script left running ends with it
    code: int | None = proc.wait()
    if timed_out or cut:
        code = None
    return Finished(code, bytes(out[:launch.limit]), bytes(err[:STDERR_BYTES]), cut, timed_out)


def _stop(proc: subprocess.Popen[bytes], keeper: bool) -> None:
    """Stop the script and what it started: the keeper is told to sweep, then the script's session and group are
    killed, which is all there is to stop off Linux."""
    try:
        if keeper and proc.poll() is None:
            os.kill(proc.pid, signal.SIGTERM)  # the keeper sweeps every descendant, then exits
            try:
                proc.wait(timeout=KEEPER_WAIT)
            except subprocess.TimeoutExpired:
                pass
    except (ProcessLookupError, PermissionError):
        pass  # the keeper has ended
    if keeper:
        _kill_session(proc.pid)  # the script's own group, which a keeper too slow to sweep leaves behind
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group has ended


def _spawn(launch: Launch, keeper: bool) -> subprocess.Popen[bytes]:
    """The script started in a session of its own, under the keeper when `keeper`, which tells through a pipe of its
    own when the program could not be executed. Raises OSError when it cannot start."""
    told_r = told_w = -1
    if keeper:
        program = launch.argv[0]
        if "/" not in program:
            found = shutil.which(program, path=launch.env.get("PATH", os.defpath))
            if found is None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), program)
        told_r, told_w = os.pipe()
        argv = [sys.executable, "-I", "-S", "-c", _KEEPER, str(told_w), *launch.argv]
    else:
        argv = launch.argv
    try:
        proc = subprocess.Popen(argv, cwd=launch.cwd, env=launch.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True,
                                pass_fds=(told_w,) if keeper else ())
    except OSError:
        if keeper:
            os.close(told_r)
        raise
    finally:
        if keeper:
            os.close(told_w)
    if keeper:
        with os.fdopen(told_r, "rb") as told:
            failed = told.read()
        if failed:
            proc.stdout.close()  # type: ignore[union-attr]
            proc.stderr.close()  # type: ignore[union-attr]
            proc.wait()
            number = int(failed)
            raise OSError(number, os.strerror(number), launch.argv[0])
    return proc


# -- the gatherer: a method for each kind of line

# What a run's grounding and repair calls are kept under in the spend log, before its agent's id.
RUN_SPEND = ("ground", "repair")
READ_AT_ONCE = 20  # the most articles one read asks for
SUMMARY_NOTE = "summary:"  # how a line that says a summary fell back to an article's first part opens


def _summary_of(article: Article, heading: str, reader: str) -> tuple[list[Any], bool]:
    """The parts a `summary:` heading loads of an article: each passage headed so, or, when none is, its first passage
    an agent wrote, not code, which the second value says. A passage the reader wrote is never loaded."""
    shown = [s for s in passages_of(article) if s.by != reader and s.text.strip()]
    found = [s for s in shown if s.heading.casefold() == heading.strip().casefold()]
    written = [s for s in shown if not s.by.startswith(MARK)] or shown
    return (found, False) if found or not shown else (written[:1], True)


class _Gatherer:
    """One gather: the agent, its lookup as that agent, the article it writes, and what it has read of it."""

    def __init__(self, agent: Agent, lookup: Lookup, this: str | None, writers: list[str], root: Path | None,
                 now: datetime | None, last: LastSets | None = None, starter: Starter | None = None,
                 zone: tzinfo | None = None, fiscal_start: int | None = None, judge: KeepJudge | None = None):
        self.agent, self.lookup, self.this, self.root, self.now = agent, lookup, this, root, now
        self.last, self.starter, self.judge = last, starter, judge
        self.zone: tzinfo = zone or getattr(lookup, "zone", None) or UTC
        self.fiscal = (lambda: (fiscal_start, None)) if fiscal_start is not None else getattr(
            lookup, "fiscal", lambda: (None, None))
        self.caller = Caller(agent.id, "agent")
        self.writers = set(writers) | {agent.id}
        self.name = f"agents/{agent.id}.md"
        self._this_article: Article | None = None
        self._runs: set[str] | None = None
        self._held: dict[tuple[str, str], list[str]] = {}

    def fail(self, n: int, line: EvidenceLine, problem: str) -> EvidenceError:
        return EvidenceError(f"{self.name}: evidence {n}, {line.kind}: {problem}")

    # -- what the agent's own runs wrote, which the digest leaves out so a run of its own never makes it due

    def ours(self, by: Caller | None) -> bool:
        return by is not None and by.kind == "agent" and by.name in self.writers

    def marks(self) -> set[str]:
        """The marks of the parts rendered in a writer's name."""
        return {MARK + writer for writer in self.writers}

    def runs(self) -> set[str]:
        """The ids of the writers' own runs, as the `agent-runs` log has them."""
        if self._runs is None:
            rows = self.lookup.query(self.caller, "agent-runs", {}).items
            self._runs = {str(r["run"]) for r in rows if r.get("agent") in self.writers and r.get("run")}
        return self._runs

    def wrote(self, source: str, row: dict[str, Any]) -> bool:
        """Whether a writer's own run wrote a log's row: its run, its spend, a proposal it filed or last decided,
        or a merge it made."""
        if source == "agent-runs":
            return row.get("agent") in self.writers
        if source == "spend":
            run = str(row.get("run") or "")
            return run in self.runs() or run in {f"{kind} {w}" for kind in RUN_SPEND for w in self.writers}
        if source == "merges":
            return row.get("by") in self.writers
        if source == "proposals":
            proposal = proposal_from(row)
            return self.ours(proposal.decided_by if proposal.status != WAITING and proposal.decided_by else proposal.by)
        return False

    def others_row(self, source: str, row: dict[str, Any]) -> dict[str, Any] | None:
        """A log's row as others left it: None for a row a writer wrote, but for a proposal a writer decided last,
        which counts as the newest row of it someone else wrote."""
        if source == "agent-runs" and gather_failed(row):
            return None  # nothing an agent could write about changed
        if not self.wrote(source, row):
            return row
        store = getattr(self.lookup, "store", None)
        if source != "proposals" or store is None:
            return None
        earlier = [r.data | {"seq": r.seq} for r in store.rows(source, {"id": row.get("id")})]
        return next((r for r in reversed(earlier) if not self.wrote(source, r)), None)

    def others_front(self, article: Article) -> dict[str, Any]:
        """An article's front matter as others left it: a field a writer of this run set last is left out, since a
        field holds only its last value."""
        front = _front(article)
        for name, prop in article.properties.items():
            if name in front and self.ours(prop.by):
                del front[name]
        return front

    def admitted(self, item: Item, thing: Any) -> Item:
        """An article of a curator's query with its keys, and who admitted it, when and why, so a curator weighs an
        archived article by the reason it was admitted with."""
        if self.agent.role != "curator" or not isinstance(thing, Article):
            return item
        who = thing.identity
        if who.keys:
            keys = f"\n  keys: {json.dumps(who.keys, sort_keys=True)}"
            item = replace(item, text=item.text + keys, counts=(item.counts or item.text) + keys)
        if who.admitted_by is None:
            return item
        said = f"\n  admitted by {who.admitted_by.name} on {who.admitted_at[:10]}"
        reason = " ".join(who.admitted_reason.split())
        reason = reason if len(reason) <= 200 else f"{reason[:200]}… ({len(reason)} characters)"
        # the reason is the curator's own words: shown, and never counted
        return replace(item, text=item.text + said + (f": {reason}" if reason else ""),
                       counts=(item.counts or item.text) + said)

    def counted(self, item: Item, thing: Any) -> Item:
        """An item of a query, with what the digest counts of an article a writer set a field of."""
        if isinstance(thing, Article) and (front := self.others_front(thing)) != _front(thing):
            item.counts = f"- [^{thing.identity.id}] {thing.identity.title}: " + json.dumps(front, sort_keys=True,
                                                                                            default=str)
        return item

    def fact(self, fact: Fact, of: Any, result: QueryResult) -> Item:
        """A fact of a query by the article `of`, labelled with how it is tied and marked by an expire of its
        document, as the query's result says."""
        item = self.tied(_item(fact), fact, of, getattr(result, "ties", {}).get(fact.id))
        return _marked(item, getattr(result, "stale", {}).get(fact.id))

    def tied(self, item: Item, fact: Fact, of: Any, how: str | None) -> Item:
        """A fact's item with a label after it when it is not plainly about the article `of` its line read it by: how
        it is tied, the title of what it is about, and the other article of `of`'s collection that holds that, each
        title quoted, so no title can read as the label's own words or start a line. A subject the fact's record names
        among the parties who are yours, its `ours`, is marked of this workspace's own side."""
        store = getattr(self.lookup, "store", None)
        if how and store is not None and isinstance(of, list):  # a line of several articles: the one tied as `how`
            of = next((id for id in of if tied(store, fact.id, [id]) == how), None)
        article = store.article(of) if how and store is not None and isinstance(of, str) else None
        if article is None:
            return item
        parts = [how if how == MENTIONS else "from this article's record" if of == self.this else
                 f"from a record of {_quoted(article.identity.title)}"]
        about = next((id for id, kept in store.ties_of(fact.id).items() if SUBJECT in kept), None)
        held = store.article(about) if about is not None else None
        if held is not None:
            parts.append(f"subject: {_quoted(held.identity.title)}")
            group = article.identity.collection
            if held.identity.collection == group:
                holders = []
            elif (about, group) in self._held:
                holders = self._held[(about, group)]
            else:
                holders = self._held[(about, group)] = held_by(store, about, group)
            if holders and of not in holders:
                parts[-1] += ", of " + " and ".join(_quoted(t.identity.title if (t := store.article(h)) else h)
                                                    for h in holders)
        elif fact.subject and any(m.role == SUBJECT and m.text == fact.subject for m in fact.mentions):
            parts.append(f"subject: {_quoted(fact.subject)}")
        subject = held.identity.title if held is not None else fact.subject
        if len(parts) > 1 and subject.strip():
            mine = (store.state(f"ours:{fact.source}") or {}).get(fact.record) or next(
                ((row.get("fields") or {}).get("ours") or [] for row in store.state(f"held:{fact.source}") or []
                 if row.get("id") == fact.record), [])
            if " ".join(subject.casefold().split()) in {" ".join(str(n).casefold().split()) for n in mine}:
                parts[-1] += ", of this workspace's own side"
        return replace(item, text=f"{item.text} [{'; '.join(parts)}]")

    # -- the articles `this` and its fields name

    def need_this(self, n: int, line: EvidenceLine) -> str:
        if self.this is None:
            raise self.fail(n, line, f"it reads the article the run writes, and agent {self.agent.id} was given "
                                     "none: run it on an article")
        return self.this

    def article(self, id: str) -> Article | None:
        result = self.lookup.read(self.caller, [id])
        return result.articles[0] if result.articles else None

    def this_article(self, n: int, line: EvidenceLine) -> Article | None:
        if self._this_article is None:
            self._this_article = self.article(self.need_this(n, line))
        return self._this_article

    def anchor(self, n: int, line: EvidenceLine, value: str) -> list[str] | None:
        """The article ids `this` and a field of it name, or None when the value names neither. A field that is
        missing, or names no article, resolves to nothing."""
        head, _, name = value.partition(".")
        if head != "this":
            return None
        if value == "this":
            return [self.need_this(n, line)]
        article = self.this_article(n, line)
        prop = fields(article).get(name) if article is not None else None
        values = prop.value if prop is not None else None
        values = values if isinstance(values, list) else [values]
        return [self.followed(_id(v)) for v in values if isinstance(v, str) and v.strip()]

    def followed(self, id: str) -> str:
        """The id a link to `id` reads: the article its merges lead to, or `id` itself."""
        store = getattr(self.lookup, "store", None)
        return follow(id, redirects(store)) if store is not None else id

    # -- query, facts, articles and files

    def query(self, n: int, line: EvidenceLine, source: str, filters: dict[str, Any], spelled: str) -> Evidence:
        limit = None
        filters, order_by = _ordered(filters)
        resolved: dict[str, Any] = {}
        for key, value in filters.items():
            if str(key).lower() == "limit":
                limit = value
                continue
            ids = self.anchor(n, line, value) if isinstance(value, str) else None
            if ids is None:
                resolved[key] = value
            elif not ids:
                entry = Evidence(spelled[:1].upper() + spelled[1:], spelled=spelled)
                entry.notes.append(f"{key} {value} names no article, so the query matches nothing")
                return entry
            else:
                resolved[key] = ids[0] if len(ids) == 1 else ids
        windowed = any(is_window(v) for v in filters.values())
        heading = spelled[:1].upper() + spelled[1:] + (f", as of {_day(self.now)}" if windowed and self.now else "")
        result = self.lookup.query(self.caller, source, resolved, order_by=order_by, descending=order_by is not None,
                                   limit=limit)
        entry = Evidence(heading, spelled=spelled)
        items = list(result.items)
        if all(isinstance(item, Fact) for item in items):
            items.sort(key=lambda fact: fact.at, reverse=True)
        of = resolved.get("articles") if source == "facts" else None
        entry.items = [self.fact(thing, of, result) if isinstance(thing, Fact) else
                       self.admitted(self.counted(_item(thing), thing), thing) for thing in items]
        if source in LOGS:
            entry.counted = [_item(row) for row in (self.others_row(source, row) for row in items) if row]
        unread = result.count - len(items)
        if unread > 0 and limit is not None:
            entry.notes.append(f"filtered by what this entry names and kept by its limit the newest {len(items)} of "
                               f"the {result.count} it matched, so {unread} were not read")
        elif unread > 0:
            entry.notes.append(f"{unread} more matched and were not returned: query with a narrower filter to read "
                               "them")
        entry.notes += result.warnings
        return entry

    def facts(self, n: int, line: EvidenceLine) -> Evidence:
        args = dict(line.args or {})
        every = args.get("latest") is False or str(args.get("latest")).strip().lower() == "false"
        shown = {k: v for k, v in args.items() if not (k == "latest" and every)}
        spelled = "facts" + (f", {_describe(shown)}" if shown else "") + (
            ", every revision of each record" if every else "")
        entry = self.query(n, line, "facts", {"articles": "this", **args}, spelled)
        if "articles" not in args and self.this is not None:
            entry.notes += self.retired(self.this)
        return entry

    def retired(self, article_id: str) -> list[str]:
        """A line for each field the article holds whose fact, from one of its records, was retired with none
        standing for that field since: its record no longer gives it. Labelled for the writer, who decides what the
        article's field becomes; never something to cite."""
        store, extraction = getattr(self.lookup, "store", None), getattr(self.lookup, "extraction", None)
        article = store.article(article_id) if store is not None else None
        if article is None or extraction is None or not article.properties:
            return []
        linked = extraction.facts(ids=store.facts_of(article_id))
        standing = {(f.record, f.field) for f in linked if f.field and f.status == "current"}
        gone: dict[tuple[str, str], Fact] = {}
        for fact in sorted(linked, key=lambda f: f.at):
            if fact.kind == FIELD_KIND and fact.field in article.properties and fact.status != "current" and \
                    (fact.record, fact.field) not in standing:
                gone[(fact.record, fact.field)] = fact
        return [f"retired: {fact.text} Its record no longer gives the field {fact.field}, so this fact no longer "
                "stands." for fact in gone.values()]

    def articles(self, n: int, line: EvidenceLine) -> Evidence:
        summary, text_only = line.options.get("summary"), bool(line.options.get("text_only"))
        spelled = "articles " + ", ".join(line.args) + (f", summary {summary}" if summary else "") + (
            ", text_only" if text_only else "")
        entry = Evidence(f"Articles {', '.join(line.args)}", spelled=spelled)
        ids: list[str] = []
        for name in line.args:
            anchored = self.anchor(n, line, name)
            if anchored == []:
                entry.notes.append(f"{name} names no article, so this line reads nothing from it")
            if anchored is not None:
                ids += anchored
            elif "/" not in name.strip().strip("/"):
                result = self.lookup.query(self.caller, name.strip().strip("/"), {})
                ids += [a.identity.id for a in result.items if isinstance(a, Article)]
            else:
                ids.append(self.followed(_id(name)))
        ids = list(dict.fromkeys(ids))
        articles: list[Article] = []
        for start in range(0, len(ids), READ_AT_ONCE):
            result = self.lookup.read(self.caller, ids[start:start + READ_AT_ONCE])
            found = {article.identity.id: article for article in result.articles}
            for id in ids[start:start + READ_AT_ONCE]:
                if id not in found:
                    entry.notes.append(f"articles/{id}.md does not exist yet")
                    continue
                article = found[id]
                articles.append(article)
                own = id == self.this
                if summary and not own:
                    entry.items.append(self.summary(article, summary, entry.notes))
                    continue
                entry.items.append(self.front_and_text(article, own))
                if not text_only:
                    facts = self.lookup.query(self.caller, "facts", {"articles": id})
                    entry.items += [self.fact(f, id, facts)
                                    for f in sorted(facts.items, key=lambda f: f.at, reverse=True)]
        return self.keep(n, line, entry, articles) if "keep" in line.options else entry

    def keep(self, n: int, line: EvidenceLine, entry: Evidence, articles: list[Article]) -> Evidence:
        """An articles entry cut by its `keep` and `top`. Each article but the one the run writes is cut into
        passages, its front matter and each block of the parts it loads; the judge scores each against the
        instruction; and `choose` keeps those at the bar, the best `top` of them. An article with none kept does not
        load. A line before the articles counts every passage whole, so a change to any of them moves the digest.
        With no judge, or a judge that scored none of the passages, the entry loads as it would without keep, and a
        note says why. A passage left unscored while others were scored counts as under the bar, and a note says how
        many were."""
        instruction, summary = str(line.options["keep"]).strip(), line.options.get("summary")
        cut: dict[str, list[tuple[Passage, Any]]] = {}
        for article in articles:
            id = article.identity.id
            if id == self.this:
                continue  # the article the run writes is never cut
            if summary:
                parts = _summary_of(article, str(summary), self.agent.id)[0]
            else:
                parts = [s for s in passages_of(article) if s.by != self.agent.id]
            cut[id] = [(Passage(id, "", _front_text(article), front=True), None)]
            cut[id] += [(Passage(id, s.heading, block), s) for s in parts for block in blocks(s.text)]
        passages = [p for pairs in cut.values() for p, _ in pairs]
        if not passages:
            return entry
        bar = float(line.options.get("keep_bar", self.judge.bar if self.judge is not None else KEEP_BAR))
        scored = self.judge.score(instruction, passages, bar) if self.judge is not None else KeepScores(
            [None] * len(passages), "no decision model was handed to this run's evidence")
        unscored = sum(score is None for score in scored.scores)
        if unscored == len(passages):
            entry.notes.append(f"keep: the judge left passages unscored, so this entry loaded as it would without "
                               f"keep: {scored.wait or 'it gave no reason'}")
            return entry
        for passage, score in zip(passages, scored.scores):
            passage.score = score
        choose(passages, line.options.get("top"), bar)
        whole = hashlib.sha256("\0".join(f"{p.article}\0{p.heading}\0{p.text}" for p in passages).encode()).hexdigest()
        items = [Item(f"keep:{n}:{whole[:16]}", KEEP_LINE)]
        by_id = {article.identity.id: article for article in articles}
        for item in entry.items:
            id = item.key.split(":")[1] if item.key.startswith(("article:", "summary:")) else ""
            if id not in cut:
                items.append(item)  # the article the run writes, whole, and its facts
            elif kept := [(p, s) for p, s in cut[id] if p.kept]:
                items.append(_kept_article(by_id[id], kept, len(cut[id])))
        entry.items = items
        total, chosen = len(passages), sum(1 for p in passages if p.kept)
        entry.notes.append(f"keep: kept {chosen} of {total} passages and dropped {total - chosen}, {scored.scored} "
                           f"scored now and {scored.cached} from the cache")
        if unscored:
            entry.notes.append(f"keep: the judge left {unscored} of the {total} passages unscored, so they "
                               f"counted as under the bar: {scored.wait or 'it gave no reason'}")
        return entry

    def front_and_text(self, article: Article, own: bool) -> Item:
        """An article as its front matter and its text, without the parts its reader wrote, and on the article the
        run writes, without those of any writer of the run."""
        id = article.identity.id
        parts = [s for s in article.compilations if s.by not in (self.writers if own else {self.agent.id})]
        label = f"### Article articles/{id}.md" + (", the one you write, without your own text" if own else "")
        return self.article_item(f"article:{id}", label, article, parts)

    def summary(self, article: Article, heading: str, notes: list[str]) -> Item:
        id = article.identity.id
        found, first = _summary_of(article, heading, self.agent.id)
        if first:
            took = f"its first part, {found[0].heading}," if found[0].heading else "its first part"
            what = f"its front matter and {took.rstrip(',')}, since no part of it is headed {heading}"
            notes.append(f"{SUMMARY_NOTE} articles/{id}.md has no part headed {heading}, so {took} loaded in its place")
        else:
            what = f"its front matter and its {heading} part" if found else (
                "its front matter alone, since nothing is written on it yet")
        label = f"### Article articles/{id}.md, summary: {what}"
        return self.article_item(f"summary:{id}:{heading.casefold()}", label, article, found)

    def article_item(self, key: str, label: str, article: Article, parts: list[Any]) -> Item:
        """An article under `label` with `parts` of its text, and what the digest counts of it: neither a field a
        writer of this run set last nor a part rendered in a writer's name."""
        text = _article_text(label, _front(article), parts)
        counts = _article_text(label, self.others_front(article), [s for s in parts if s.by not in self.marks()])
        return Item(key, text, _cited(article, parts), left=f"articles/{article.identity.id}.md",
                    counts=None if counts == text else counts)

    def files(self, n: int, line: EvidenceLine) -> Evidence:
        if self.root is None:
            raise self.fail(n, line, "files reads the workspace's folder, and gather was given none: pass root")
        entry = Evidence(f"Files {', '.join(line.args)}", spelled="files " + ", ".join(line.args))
        base = self.root.resolve()
        for name in line.args:
            path = self.root / name
            paths = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for p in paths:
                rel = p.relative_to(self.root).as_posix()
                if any(part.startswith(".") for part in Path(rel).parts):
                    continue  # a hidden file, such as .env or the state folder, never reaches a prompt
                if not p.resolve().is_relative_to(base):
                    entry.notes.append(f"{rel} leads outside the workspace's folder, so it is not read")
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except FileNotFoundError:
                    entry.notes.append(f"{rel} does not exist")
                    continue
                except (OSError, UnicodeDecodeError) as error:
                    raise self.fail(n, line, f"{rel} cannot be read as UTF-8 text ({type(error).__name__}): name a "
                                             "text file")
                entry.items.append(Item(f"file:{rel}", f"### File {rel}\n\n{text.strip()}", left=rel))
        return entry

    # -- left: what a query returned at the last kept write and returns no more

    def left(self, n: int, line: EvidenceLine, earlier: set[str]) -> Evidence:
        self.need_this(n, line)
        if self.last is None:
            raise NotBuilt(f"{self.name}: evidence {n}, left cannot run here: it compares with what its query "
                           "returned at the agent's last write on this article, which compile keeps, and gather was "
                           "given none: pass last")
        if not isinstance(line.args, dict) or not line.args.get("from"):
            raise self.fail(n, line, "it names no from: write - left: { from: things, due: this_quarter }")
        source = line.args["from"]
        filters, order_by = _ordered({k: v for k, v in line.args.items() if k != "from"})
        spelled = left_key(line).removeprefix("query ")
        now = self.lookup.query(self.caller, source, filters, order_by=order_by, descending=order_by is not None)
        returns = [a.identity.id for a in now.items if isinstance(a, Article)]
        then = self.last.at
        entry = Evidence("", spelled=f"left {spelled}", returns_now=returns)
        last = list(dict.fromkeys(_id(i) for i in self.last.sets.get(left_key(line), [])))
        if not last:
            entry.notes.append("the agent keeps no set of this query on this article yet, so nothing has left: the "
                               "first write that lands keeps one")
        found: dict[str, Article] = {}
        for start in range(0, len(last), READ_AT_ONCE):
            batch = self.lookup.read(self.caller, last[start:start + READ_AT_ONCE])
            found |= {a.identity.id: a for a in batch.articles}
        for id in last:
            article = found.get(id)
            if article is None:
                continue  # gone from the workspace since
            if (id in returns) if then is None else not self.unmet(n, line, article, filters, then):
                continue
            why = self.why(n, line, article, filters, then)
            head = f"### Article articles/{id}.md"
            text = (f"{head}, shown above\n\nWhy it left: {why}." if f"article:{id}" in earlier else
                    f"{head}\n\nWhy it left: {why}.\n\n{json.dumps(_front(article), sort_keys=True, default=str)}")
            entry.items.append(Item(f"left:{id}", text, [Citation("article", id, id)], left=f"articles/{id}.md"))
        count = len(entry.items)
        on = f" on {_day(then.astimezone(self.zone))}" if then is not None else ""
        read = ", read as of that run" if then is not None else ", read as of now"
        entry.heading = (f"Left since the last run{on}, query {spelled}: {count} article{'' if count == 1 else 's'} it "
                         f"returned then that it no longer returns{read}, each as it stands now with why it left")
        return entry

    def span(self, n: int, line: EvidenceLine, value: str, then: datetime) -> Span:
        try:
            return span(value, then, self.zone, *self.fiscal())
        except WindowError as error:
            raise self.fail(n, line, str(error)) from None

    def unmet(self, n: int, line: EvidenceLine, article: Article, filters: dict[str, Any],
              then: datetime) -> list[str]:
        """What of the query an article fails, read as of `then`: `archived`, or a filter's key. An id names its
        collection, so a kept id is always of the query's."""
        failed = []
        if "status" not in filters and article.identity.status == "archived":
            failed.append("archived")
        values = {k: c.value for k, c in fields(article).items()} | {"id": article.identity.id,
                                                                     "status": article.identity.status}
        for key, want in filters.items():
            have = values.get(key)
            if is_window(want):
                met = holds(self.span(n, line, want, then), have, self.zone)
            else:
                met = compares(have, "=", _id(str(want)) if key == "id" else want)
            if not met:
                failed.append(key)
        return failed

    def why(self, n: int, line: EvidenceLine, article: Article, filters: dict[str, Any],
            then: datetime | None) -> str:
        """Each part of the query the article no longer meets, with its value now, and for a window the span it
        covered then and on which side of it a date now falls."""
        at = then or self.now
        if at is None:
            raise self.fail(n, line, "it reads its query as of now, since no time of the last run is known, and "
                                     "gather was given no now: pass now")
        parts = []
        for key in self.unmet(n, line, article, filters, at):
            if key == "archived":
                parts.append("it is archived now")
                continue
            want = filters[key]
            values = _values_of(article, key)
            now = ", ".join(values) if values else "empty"
            if not is_window(want):
                parts.append(f"{key} is now {now}, where the query asks for {want}")
                continue
            window = self.span(n, line, want, at)
            side = "outside"
            day = next((d for d in (_day_of(v, self.zone) for v in values) if d is not None), None)
            if day is not None and not holds(window, day.date().isoformat(), self.zone):
                side = "before" if window.start is not None and day + timedelta(days=1) <= window.start else "after"
            if then is None:
                parts.append(f"{key} is now {now}, {side} {want}, the query read as of now")
            else:
                parts.append(f"{key} is now {now}, {side} {want} as of the last run, {_span_text(window, self.zone)}")
        return ", and ".join(parts) or "it no longer meets the query"

    # -- script: what a program in the workspace's folder prints

    def script_fail(self, n: int, rel: str, problem: str) -> ScriptError:
        return ScriptError(f"{self.name}: evidence {n}, script {rel}: {problem}")

    def script_path(self, n: int, rel: str, base: Path) -> Path:
        """The real path of the script `rel` names, refused unless it is a regular executable file inside `base`,
        in no hidden or vendored folder, as spelled or as its links resolve."""
        slashed = rel.replace("\\", "/")
        parts = Path(slashed).parts
        if slashed.startswith("/") or ".." in parts or re.match(r"[A-Za-z]:", slashed):
            raise self.script_fail(n, rel, "is outside the workspace's folder: name a path inside it, such as "
                                           "scripts/usage.py")
        self.refuse_folders(n, rel, parts, "is")
        try:
            real = (base / slashed).resolve(strict=True)
        except (OSError, RuntimeError):
            raise self.script_fail(n, rel, "does not exist: write it, or fix the path") from None
        if not real.is_relative_to(base):
            raise self.script_fail(n, rel, "leads outside the workspace's folder through a link: put the script itself "
                                           "inside the folder")
        self.refuse_folders(n, rel, real.relative_to(base).parts, "resolves to a file")
        if not real.is_file():
            raise self.script_fail(n, rel, "is not a regular file: name a script file")
        if not os.access(real, os.X_OK):
            raise self.script_fail(n, rel, f"is not executable: run chmod +x {rel}")
        return real

    def refuse_folders(self, n: int, rel: str, parts: tuple[str, ...], verb: str) -> None:
        for part in parts:
            if part.startswith("."):
                raise self.script_fail(n, rel, f"{verb} in a hidden folder or file ({part}), which never holds a "
                                               "script: move it to a visible folder such as scripts/")
            if part.lower() in VENDORED:
                raise self.script_fail(n, rel, f"{verb} in a vendored folder ({part}), which holds code you did not "
                                               "write: move your own script to a folder such as scripts/")

    def script(self, n: int, line: EvidenceLine) -> Evidence:
        args = line.args if isinstance(line.args, dict) else {}
        rel = args.get("run")
        if not isinstance(rel, str) or not rel.strip() or set(args) != {"run"}:
            raise self.fail(n, line, "it takes run alone, the script's path in the workspace's folder: write - script: "
                                     "{ run: scripts/usage.py }")
        rel = rel.strip()
        if self.root is None:
            raise self.fail(n, line, "it runs a file in the workspace's folder, and gather was given none: pass root")
        base = self.root.resolve()
        real = self.script_path(n, rel, base)
        if self.starter is None:
            raise NotBuilt(f"{self.name}: evidence {n}, script cannot run here: it starts its script through "
                           "the process starter the caller hands in, and gather was given none: pass starter")
        launch = Launch([str(real)], base, {"PATH": os.environ.get("PATH") or os.defpath, WORKSPACE_ENV: str(base)})
        try:
            done = self.starter(launch)
        except OSError as error:
            raise self.script_fail(n, rel, f"could not start ({error.strerror or error}): give it a #! line, such "
                                           "as #!/bin/sh, and make it executable") from None
        if done.timed_out:
            raise self.script_fail(n, rel, f"ran past {launch.seconds:g} seconds and was stopped: make it finish "
                                           "sooner")
        if done.code != 0 and not done.cut:
            how = ("was stopped before it exited" if done.code is None else
                   f"was stopped by signal {-done.code}" if done.code < 0 else f"exited {done.code}")
            said = next((x.strip() for x in done.stderr.decode("utf-8", "replace").splitlines() if x.strip()), "")
            said = f", saying {one_line(said, 200)}" if said else ", and wrote nothing to stderr"
            raise self.script_fail(n, rel, f"{how}{said}: run it by hand in the workspace's folder to see why, and fix "
                                           "it")
        text = _decoded(done.stdout[:launch.limit], done.cut)
        if text is None:
            raise self.script_fail(n, rel, "printed text that is not UTF-8: have it print UTF-8 text")
        entry = Evidence(f"Script {rel}, what it printed", spelled=f"script {rel}")
        paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n")) if p.strip()]
        for i, paragraph in enumerate(paragraphs, 1):
            cid = f"script:{rel}:{i}"
            entry.items.append(Item(cid, f"- [^{cid}] {paragraph}", [Citation("record", cid, f"script:{rel}")]))
        if done.cut:
            entry.notes.append(f"it printed more than {launch.limit} bytes, so it was stopped and what came after is "
                               "not here: have it print less")
        if not paragraphs:
            entry.notes.append("it printed nothing")
        return entry

    # -- sql: what one read-only query of a database source gives

    def sql(self, n: int, line: EvidenceLine, sources: Mapping[str, Connector]) -> Evidence:
        args = line.args if isinstance(line.args, dict) else {}
        name, query = args.get("source"), args.get("query")
        if set(args) != {"source", "query"} or not isinstance(name, str) or not isinstance(query, str):
            raise self.fail(n, line, "it takes source and query alone: write - sql: { source: warehouse, query: "
                                     '"select kind, count(*) from things group by kind" }')
        source = sources.get(name)
        if source is None:
            raise SqlError(f"{self.name}: evidence {n}, sql: the workspace has no source {name}: add sources/{name}.yaml "
                           "with kind: database, or name a source it has")
        select = getattr(source, "select", None)
        if not callable(select):
            raise SqlError(f"{self.name}: evidence {n}, sql: source {name} is not a database source: name a source of "
                           "kind database")
        try:
            rows: Rows = select(query)
        except CerebroError as error:
            raise SqlError(f"{self.name}: evidence {n}, sql {name}: {error}") from None
        query = " ".join(query.split())
        entry = Evidence(f"Sql {name}, the rows of {query}", spelled=f"sql {name}: {query}")
        for i, row in enumerate(rows.rows, 1):
            cid = f"sql:{name}:{i}"
            entry.items.append(Item(cid, f"- [^{cid}] {row_text(rows.columns, row)}",
                                    [Citation("record", cid, f"sql:{name}")]))
        if rows.more:
            entry.notes.append(f"it gave more than {len(rows.rows)} rows, so the rest were not read: aggregate or "
                               "filter in the query")
        if rows.cut:
            entry.notes.append(f"its rows ran past the size a query gives after {len(rows.rows)}, so the rest were "
                               "not read: select fewer columns or rows")
        if not rows.rows and not rows.cut:
            entry.notes.append("it gave no rows")
        return entry

    def function(self, name: str, fn: Callable[..., Any], line: EvidenceLine) -> Evidence:
        args = {k: (self.this if v == "this" else v) for k, v in (line.args or {}).items()} \
            if isinstance(line.args, dict) else {}
        entry = Evidence(f"{name.capitalize()}" + (f", {_describe(args)}" if args else ""),
                         spelled=f"{name} {_describe(args)}".strip())
        for thing in fn(**args) or []:
            if isinstance(thing, File):
                entry.files.append(thing)
                entry.items.append(Item(f"file:{name}:{thing.name}", f"- file {thing.name} ({thing.media_type})"))
                continue
            if not isinstance(thing, dict) or "id" not in thing or "text" not in thing:
                raise EvidenceError(f"evidence function {name} returned {thing!r}: return dicts with id and text")
            entry.items.append(Item(f"record:{thing['id']}", f"- [^{thing['id']}] {thing['text']}", [Citation(
                "record", str(thing["id"]), str(thing.get("source", name)), str(thing.get("quote", "")))]))
        return entry


def _values_of(article: Article, key: str) -> list[str]:
    """The values an article's front matter holds under `key`, as text: its id, or a property's scalar or the
    scalars of its list. A null is no value."""
    if key == "id":
        return [article.identity.id]
    prop = fields(article).get(key)
    value = prop.value if prop is not None else None
    items = value if isinstance(value, list) else [value]
    return [json.dumps(v) if isinstance(v, bool) else str(v) for v in items
            if v is not None and not isinstance(v, (dict, list))]


def _day_of(value: str, zone: tzinfo) -> datetime | None:
    """The start of the day a date or a time names, in `zone`."""
    read = read_time(value, zone)
    if read is None:
        return None
    moment = read[0].astimezone(zone)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _span_text(window: Span, zone: tzinfo) -> str:
    """The days a window covers, as `Oct 1 to Dec 31, 2026`, or `before Oct 12, 2025` with no start."""
    end = window.end.astimezone(zone)
    if window.start is None:
        return f"before {_date(end)}"
    start, last = window.start.astimezone(zone), (end - timedelta(microseconds=1))
    return (f"{start:%b} {start.day} to {_date(last)}" if start.year == last.year
            else f"{_date(start)} to {_date(last)}")
