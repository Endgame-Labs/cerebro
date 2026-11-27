"""One compile: one agent's run on one article, from the evidence it reads to what lands of what it handed in.

A run is due when anything it reads moved: the digest of its evidence is kept once it finishes and stamped on what
it writes, so the same evidence does not run it again. A run, or a gather, that failed on the same evidence waits,
30 seconds doubling to an hour. A scheduled compile runs even when nothing moved.

Every run goes through `run_agent`, so each is held to the cap and leaves its spend and its `agent-runs` row. An
editor (`ground.Editor`) scores each sentence a run hands in against what it read, and the run is told, once, of
each sentence under its floor: `min_grounding` in its agent file, else in `settings.yaml`, else `ground.FLOOR`. What
it hands in lands whole in one write (`_land`), each sentence still under the floor marked, with the fields it set
with `override`, or not at all; each sentence's score and why stay on its run's row of the `agent-runs` log. A run
writes only its own part, its fields, and the article's identity only when a fixed article is first born. A run that
lands nothing leaves the article as it was, but for a line whose facts were all withdrawn. Then the mirror copy is
written. The store is the truth: a copy that fails to write is a warning, and the article stands.

Grounding is given the decision model when the workspace has one, the line the run read for each citation, the facts
linked to the article, the day, and the body of `workspace.md`. What its calls spent is kept under compilation, call
by call, each try of a call checked against the cap before it starts.

What a file writes follows its role. A writer writes its one article. An article agent (`article.md`) and a
query target write each article `targets` lists, one compile each, named by `this`. An agent that lists `subagents:`
is one run of a whole article, orchestrated here (`_Composing`) and never by a model: each subagent whose evidence
moved returns its content, kept as material. With instructions of its own, the agent then runs on every subagent's
content. With nothing new, or no instructions of its own, the content stands, joined in the order `subagents:` lists.

A curator is run by `curate`, and a subagent only inside its composer's run: compile refuses both, as it refuses a
tool it does not build, with one line that names the file.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from functools import partial
from pathlib import Path
from typing import Any

from . import prompts, rendered
from .agent import Agent, load_agent, load_agents
from .apps import bind
from .article import (
    PROTECTED,
    Article,
    Citation,
    Compilation,
    Identity,
    Property,
    body,
    mirror,
    pieces,
    protected,
)
from .budget import LOG, BudgetSpent, Held, HeldForRoom, Ledger, Stopped, stopping, under
from .caller import Caller
from .clean import label, land_heading, underlined_as_atx, unlabeled
from .clock import Clock, stamp, system_clock
from .connector import Connector
from .errors import CerebroError
from .evidence import (
    CITE,
    GATHER,
    SUMMARY_NOTE,
    DecisionKeepJudge,
    Evidence,
    Item,
    LastSets,
    Starter,
    _front,
    digest,
    digest_of,
    fingerprint,
    gather,
)
from .facts import Fact
from .ground import FLOOR, Editor, Judge, Verdict, fields_of, own_sides, quote_for, tallied, unmarked
from .identity import in_working_set
from .lookup import LINKED, MAX_READ, Lookup
from .models import Decider, ModelSpec, TokenCount, cost, price_of, read_model
from .records import RETRACTS, proposals
from .reply import outside
from .run import REVIEW, RUN_ID, STANDS, STOPPED, WROTE_NOTHING, AgentRun, Run, Usage, masked, new_run_id, tokens
from .secrets import MARK
from .store import PropertyError, Store, StoreError, check_value, own_key, update_state
from .tokens import MASKED
from .workspace import Workspace

__all__ = ["CompileError", "CompileRefused", "Compiled", "agent_surface", "birth", "compile_article", "drop_material",
           "fixed", "keep_judge", "material_key", "run_agent", "targets"]

ARTICLE_ID = re.compile(r"[\w.@-]+(?:/[\w.@-]+)+")  # a collection and a name, as notes/weekly
MAX_VALUE = 4096  # bytes of one value `override` sets, as JSON: long text belongs in the article
DRAFTS = "What your subagents returned"
BY_TOOL = "Facts you read through your tools"
INBOX = "What people asked of you"


class CompileError(CerebroError, ValueError):
    """A compile was asked for what its agent does not write, such as an article agent with no article named."""


class CompileRefused(CerebroError, NotImplementedError):
    """An agent compile does not run, by its role or a tool it lists. The message names the file and what to do."""


KEPT_LAST = "so it kept what it wrote last"  # what a part its agent did not write again says


@dataclass
class Compiled:
    """A run, the article it landed if any, a warning line when the mirror copy was not written, and a line for each
    thing that did not land. With nothing moved since the agent's last write, `run` is None and `skipped` is true.
    `run` is the agent's own run, or the subagent's run that failed; `runs` holds every run the compile made, each
    subagent's first, and `notes` what the run line should say, such as a composer that did not write."""

    run: Run | None
    article: Article | None
    warning: str = ""
    skipped: bool = False
    waiting: str = ""  # why a skipped compile waits, when it is not that nothing moved
    dropped: list[str] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rendered: list[str] = field(default_factory=list)  # the headings code wrote from `rendered:` on a skip
    left_alone: str = ""  # why no agent ran: the article is out of the working set
    grounding: dict[str, int] = field(default_factory=dict)  # what grounding did, counted by kind
    cleaned: list[str] = field(default_factory=list)  # a line for each change landing made to the text, counted


def made_safe(n: int) -> str:
    """How many changes landing made to an article's text to make it safe, as a count alone: made 3 safety edits."""
    return f"made {n} safety edit{'' if n == 1 else 's'}"


def agent_surface(lookup: Lookup, caller: Caller, seen: list[Fact | Article] | None = None) -> list[Callable[..., Any]]:
    """`query` and `read` as a context agent calls them, under its own caller. Each fact and article either returns
    is added to `seen`, when given: a run may cite what its tools returned, as it may its evidence."""

    def query(source: str, filters: dict[str, Any] | None = None, limit: int = 50) -> dict[str, Any]:
        """Search or filter the articles of a collection, the facts, or a log, by their fields. A fact or an
        article it returns may be cited by its id, as evidence is."""
        result = lookup.query(caller, source, filters, limit=limit)
        if seen is not None:
            seen.extend(item for item in result.items if isinstance(item, (Fact, Article)))
        return outside(result)

    def read(ids: list[str], about: str | None = None) -> dict[str, Any]:
        """Articles whole, each as its text with the ids it cites in place and the facts learned since it was last
        written, and facts asked for by id, for up to 20 ids: read a fact by the id an article's text names for its
        words. about gives an article's facts that hold its words. An article or a fact whose id it returns may be
        cited by it."""
        got = lookup.read(caller, ids, about, linked=False)
        shown = outside(got)
        if seen is not None:
            said = json.dumps(shown)
            seen.extend(got.articles)
            seen.extend(f for f in lookup.read(caller, ids, about).facts if f.id in said)
        return shown

    return [query, read]


def _name(agent: Agent) -> str:
    return f"agents/{agent.id}.md"


def _refuse_role(agent: Agent) -> None:
    if agent.role == "curator":
        raise CompileRefused(f"{_name(agent)} is a curator, which decides which articles exist and writes none: run it "
                             "with curate, not compile")
    if agent.role == "subagent":
        composer = agent.composers[0] if agent.composers else f"{agent.target}/article"
        raise CompileRefused(f"{_name(agent)} returns one part of an article inside the run of agents/{composer}.md: "
                             "compile that agent, which runs it")


def _inbox(store: Store, agent: Agent, article: str) -> list[Evidence]:
    """What people left for this agent, as one evidence entry, or none: each accepted instruction for its article, its
    collection or the agent itself, and each waiting proposal about its article, oldest first. It
    counts in the digest, so a new one makes the agent due, and it is picked up at its next run, like inbound mail.
    Only a person's instruction may be cited, by the record that carried it: a curator's is context and never a
    source, and a waiting proposal is an ask, not a fact."""
    from .curate import standing  # imported here: curate runs its agents through this module

    collection = article.rpartition("/")[0]
    curator = standing(store, article)
    found = [p for p in proposals(store)
             if (p.kind == "article.instruct" and p.status == "accepted" and p.target in (article, collection, agent.id)
                 and (p.by.kind != "agent" or (curator is not None and p.id == curator.id)))
             or (p.status == "waiting" and p.target == article)]
    if not found:
        return []
    items = []
    for p in found:
        said = str(p.payload.get("text") or p.reason or p.kind)
        what = "instruction" if p.kind == "article.instruct" else f"waiting {p.kind.partition('.')[2] or p.kind}"
        what = f"the curator's {what}, context and never a source," if p.by.kind == "agent" and \
            p.kind == "article.instruct" else what
        record = p.cites[0] if p.kind == "article.instruct" and p.by.kind == "person" and p.cites else ""
        shown = f"[^{record}]" if record else f"[{p.id}]"
        text = f"- {shown} {what} from {p.by.name} ({p.by.kind}), {p.at[:10]}: {said}"
        cites = [Citation("record", record, record, said)] if record else []
        items.append(Item(f"proposal:{p.id}:{p.status}", text, cites))
    return [Evidence(INBOX, [item.text for item in items], [c for item in items for c in item.citations], items=items,
                     spelled="inbox")]


def _read(agent: Agent, article: str) -> str:
    """The state key that keeps the digest an agent's last finished run read on an article, whether or not anything of
    it landed, so evidence that has not moved does not run it again."""
    return f"compiled:{agent.id}:{article}"


def fixed(agent: Agent) -> bool:
    """Whether the agent writes one article of its own, which a pass births before its first run, and a compile run
    by hand at its first landed write."""
    return agent.role == "writer" or (agent.role == "composer" and "/" in agent.target)


def targets(agent: Agent, lookup: Lookup) -> list[str]:
    """The articles an agent writes, one compile each: a fixed article's own path, every article of an article agent's
    collection, or every article a query target matches. An article out of the working set, archived or merged
    away, is none of them, a fixed article's own included; one not born yet is, since its first run births it."""
    _refuse_role(agent)
    if fixed(agent):
        return [] if left_alone(agent, lookup) else [agent.target]
    filters = {k: v for k, v in (agent.target_query or {}).items() if k != "from"}
    result = lookup.query(Caller(agent.id, "agent"), agent.target, filters)
    return [item.identity.id for item in result.items if isinstance(item, Article)]


def left_alone(agent: Agent, lookup: Lookup) -> str:
    """Why an agent that `targets` gives no article to has nothing to run, or "": its own article is archived, or a
    merge folded it away, or every article of its target is archived, one by its path or how many."""
    caller = Caller(agent.id, "agent")
    if fixed(agent):
        found = lookup.read(caller, [agent.target]).articles  # a read follows a merge to the article it made
        return out_of_working_set(agent.target, found[0].identity if found else None)
    filters = {k: v for k, v in (agent.target_query or {}).items() if k != "from"} | {"status": "archived"}
    out = [item.identity.id for item in lookup.query(caller, agent.target, filters).items if isinstance(item, Article)]
    if len(out) > 1:
        return f"{len(out)} articles out of the working set left alone"
    return f"{out[0]} is out of the working set, left alone" if out else ""


def out_of_working_set(article: str, found: Identity | None) -> str:
    """Why no agent runs on `article`, as `found` stands, or "": it is archived, or a merge folded it into another, so
    the article found under its path is another one or merged. One not born yet is none of these."""
    if found is None or (found.id == article and found.status not in ("archived", "merged")):
        return ""  # archived or merged away is what takes an article out of the working set, as `in_working_set` says
    how = "archived" if found.id == article and found.status == "archived" else "out of the working set"
    return f"{article} is {how}, left alone"


def _article(agent: Agent, this: str | None) -> str:
    """The article this compile writes."""
    if fixed(agent):
        if this not in (None, agent.target):
            raise CompileError(f"{_name(agent)} writes articles/{agent.target}.md alone, not {this}: compile it "
                               "without this")
        return agent.target
    what = "each article its target: query matches" if agent.role == "query" else "each article of its collection"
    if not this:
        raise CompileError(f"{_name(agent)} writes {what}, {agent.target}: name the one to compile with this, as "
                           "targets lists them")
    if this.split("/")[0] != agent.target:
        raise CompileError(f"{_name(agent)} writes {what}, {agent.target}, and {this} is not in it: name an article "
                           f"under articles/{agent.target}/")
    return this


def _tools(agent: Agent, lookup: Lookup, agent_tools: dict[str, Callable[..., Any]],
           workspace: Workspace | None, seen: list[Fact | Article]) -> list[Callable[..., Any]]:
    """The tools the agent file names, bound with its caller and the workspace handle, which the model never sees. The
    facts and articles `query` and `read` return go into `seen`."""
    caller = Caller(agent.id, "agent")
    tools = agent_surface(lookup, caller, seen)
    for name in agent.tools:
        if name in agent_tools:
            tools.append(bind(agent_tools[name], caller, workspace))
    return tools


def field_refused(article: str, name: str, value: Any) -> str:
    """Why a writer may not set the front matter field `name` to `value` on `article`, or "" when it may:
    the guard an article's agents and its curator write under alike. A protected field is the system's."""
    if not name:
        return "a field with no name is not yours to set: name the field, such as level"
    if kept := protected(name):
        return (f"{name} is the system's, not yours to set: {PROTECTED[kept]}, so a value you set there never "
                "stands: write what you would change in the article")
    if len(json.dumps(value, default=str).encode()) > MAX_VALUE:
        return f"the value of {name} is over {MAX_VALUE} bytes: put long text in the article"
    if name == LINKED and value is not None and not (isinstance(value, list) and all(
            isinstance(v, str) and ARTICLE_ID.fullmatch(v) and not v.lower().endswith(".md")
            and not v.lower().startswith("articles/")
            and all(part.strip(".") for part in v.split("/")) for v in value)):
        return (f'{LINKED} is a list of article ids, such as ["projects/atlas"], and '
                f"{json.dumps(value, default=str)[:200]} is not: give each id alone, with no brackets, link, articles/ or .md")
    try:
        check_value(article, name, value)
    except PropertyError as error:
        return str(error)
    return ""


class _Writes:
    """What one run writes besides what it hands in, held until that lands: the fields it sets."""

    def __init__(self, article: str, store: Store, by: str):
        self.article, self.store, self.by = article, store, Caller(by, "agent")
        self.sets: list[tuple[str, Any, list[str]]] = []

    def tools(self) -> list[Callable[..., Any]]:
        def override(field: str, value: str | bool | int | float | list[str] | None,  # noqa: PYI041
                     cites: list[str] | None = None) -> str | dict[str, str]:
            """Set one front matter field of the article you write, such as level, citing the evidence ids it rests
            on, or drop it with a null value. It lands with the text you hand in. Every field of what the article
            holds is yours to set or drop, but the ones the wiki keeps itself: id, title, status, compiled_at,
            identity, keys and in_source. A call it refuses answers with refused and why."""
            name = field.strip()
            if why := field_refused(self.article, name, value):
                return {"refused": f"override: {why}"}  # as a refused call answers, so it counts as one
            self.sets.append((name, value, list(cites or [])))
            return f"dropped {name}" if value is None else f"set {name}"

        return [override]

    def land(self, at: str) -> None:
        """Write each field the run set, replacing the value it held, and take off each it dropped. A value the field
        holds already keeps who set it first and when, with the run's cites added."""
        for name, value, cites in self.sets:
            if value is None:
                self.store.drop_field(self.article, name)
                continue
            prop = Property(value, self.by, at, cites)
            key, written = self.store.valued(name, prop)  # as the store would keep it
            held = self.store.article(self.article)  # as the run's earlier sets left it
            was = held.properties.get(key) if held is not None else None
            if was is not None and type(was.value) is type(written.value) and was.value == written.value:
                prop = replace(was, cites=list(dict.fromkeys([*was.cites, *cites])))
            self.store.set_field(self.article, name, prop)


def _by_tool(read: list[Fact | Article]) -> list[Evidence]:
    """The facts a run read through its tools, as one more entry of its evidence, so what it cites of them is checked
    as any evidence is, and its digest moves when one of those facts does. Empty when it read no fact that way."""
    facts = [f for f in read if isinstance(f, Fact)]
    if not facts:
        return []
    entry = Evidence(BY_TOOL)
    for fact in sorted({f.id: f for f in facts}.values(), key=lambda f: f.id):
        quote = quote_for(fact)
        text = f"- [^{fact.id}] {fact.text}" + (f' (quote: "{quote}", {fact.record})' if quote else "")
        entry.items.append(Item(f"fact:{fact.id}:{fact.status}", text, [Citation("fact", fact.id, fact.record, quote)],
                                fact.at, fact.record))
        entry.lines.append(text)
        entry.citations.append(Citation("fact", fact.id, fact.record, quote))
    return [entry]


def _articles_read(read: list[Fact | Article]) -> list[Evidence]:
    """The articles a run read through its tools, as an entry that lets it cite them, each line its title, fields and
    text, as the run read it, which counts in no digest: an article moves at every write, and its facts move a
    reader's digest already."""
    found = {a.identity.id: a for a in read if isinstance(a, Article)}
    if not found:
        return []
    lines = [f"- [^{id}] {a.identity.title}: {json.dumps(_front(a), sort_keys=True, default=str)}\n\n{body(a)}"
             for id, a in sorted(found.items())]
    return [Evidence(BY_TOOL, lines, citations=[Citation("article", id, id) for id in sorted(found)],
                     items=[Item(f"article:{id}", line, [Citation("article", id, id)])
                            for id, line in zip(sorted(found), lines)])]


def _tool_reads(agent: Agent, article: str) -> str:
    """The state key that keeps the ids of the facts an agent's last run on an article read through its tools."""
    return f"by-tool:{agent.id}:{article}"


def _read_before(lookup: Lookup, store: Store, agent: Agent, article: str) -> list[Evidence]:
    """The facts the agent's last run on the article read through its tools, as they stand now, for its digest."""
    ids = store.state(_tool_reads(agent, article)) or []
    if not ids or lookup.extraction is None:
        return []
    return _by_tool(lookup.extraction.facts(ids=ids))


def _left_state(agent: Agent, article: str) -> str:
    """The state key that keeps what each `left` line of an agent's returned at its last kept write on an article."""
    return f"left:{agent.id}:{article}"


def _last_sets(store: Store, agent: Agent, article: str) -> LastSets:
    """What the agent's `left` lines returned at its last kept write on the article, and when that run read them."""
    kept = store.state(_left_state(agent, article)) or {}
    at = kept.get("at")
    return LastSets(dict(kept.get("sets") or {}), datetime.fromisoformat(at) if at else None)


def _keep_left(store: Store, agent: Agent, article: str, evidence: list[Evidence], at: str) -> None:
    """Once a write lands, keep what each of the agent's `left` lines returns now, with when its run read it, for
    the next run to say what left since."""
    sets = {"query " + e.spelled.removeprefix("left "): e.returns_now for e in evidence if e.returns_now is not None}
    if sets:
        store.set_state(_left_state(agent, article), {"sets": sets, "at": at})


def _citable(*evidence: list[Evidence]) -> dict[str, Citation]:
    return {c.id: c for entries in evidence for entry in reversed(entries) for c in entry.citations}


def _model(agent: Agent, model: ModelSpec | None) -> ModelSpec | None:
    return read_model(agent.model, f"{_name(agent)} model") if agent.model else model


@dataclass
class _Part:
    """One part to land: its heading, or for a part written whole the writer's id, the agent whose mark it takes,
    its text, what it may cite, when the run that wrote it started and ended, whether it is written whole, and the
    digest it is stamped with, when not the one the whole landing gives."""

    heading: str
    by: str
    text: str
    citable: dict[str, Citation]
    started: str
    at: str
    whole: bool = False
    digest: str | None = None


def _landing(editor: Editor, agent: Agent, text: str) -> tuple[str, list[str]]:
    """Text handed in whole as it lands, an underlined heading read as the `#` heading it shows as: each sentence under
    the floor marked, and each part under a heading the agent's `rendered:` names taken out, since the wiki writes
    that heading itself, with a line that says so."""
    text = underlined_as_atx(text)
    rendered = {h.casefold() for h in agent.rendered}
    found = pieces(text)
    gone = [p for p in found if p.level and p.heading.casefold() in rendered]
    if gone:
        text = "\n\n".join(f"{p.line}\n\n{p.under}".rstrip() if p.level else p.under for p in found if p not in gone)
    return editor.marked(text).strip("\n"), [f"{p.heading}: dropped the part: the wiki writes {p.heading} itself"
                                             for p in gone]


class _LeftAlone(Exception):
    """The article left the working set while its run was under way, so what the run wrote lands nowhere."""


def _land(store: Store, article: str, parts: list[_Part], digest_: str, writers: list[str],
          after: Callable[[], None]) -> tuple[list[str], list[str]]:
    """Write each part in order, after every other part of the article, stamped with its own digest or `digest_`,
    and delete each part one of `writers` left before that no part replaced.

    The parts land together in one write, with what `after` writes, or not at all: a process stopped part way leaves
    the article as it was, not old and new mixed. Each part is masked and cleaned as the store writes it
    (`Store.landed`), and a line for each thing cleaning changed is given, then each it noted and left, but only for
    a part whose text or footnotes land other than they stand, so a part landed the same on every pass says it once."""
    current = store.article(article)
    standing = {s.heading: (s.text, s.citations) for s in current.compilations} if current is not None else {}
    written = {part.heading for part in parts} | {_heading(store, part.heading) for part in parts if not part.whole}
    gone = [s.heading for s in (current.compilations if current is not None else [])
            if s.by in writers and s.heading not in written]
    changed: list[str] = []
    noted: list[str] = []
    landing = []
    for part in parts:
        landed, edits, notes = store.landed(Compilation(article, part.heading, part.text, by=part.by,
                                                   digest=digest_ if part.digest is None else part.digest, at=part.at,
                                                   citations=_cited(part.text, part.citable), started=part.started,
                                                   whole=part.whole))
        if standing.get(landed.heading) != (landed.text, landed.citations):
            under = "" if part.whole else f"under {part.heading}: "
            changed += [under + line for line in edits]
            noted += [under + line for line in notes]
        landing.append(landed)
    with store.exclusive():
        found = store.article(article)
        if why := out_of_working_set(article, found.identity if found is not None else None):
            raise _LeftAlone(why)  # a person archived it while the run was under way: nothing of the run lands
        for compilation in landing:
            store.delete_compilation(article, compilation.heading)  # written again last, so the parts stand in the order given
            store.write_compilation(compilation)
        for heading in gone:
            store.delete_compilation(article, heading)
        after()
        unmirrored(store, article, True)  # with the parts: a copy cut off before it was written is written next pass
    return changed, noted


HEADING_LINE = re.compile(r"#{1,6}(\s|$)")  # a markdown heading's line, which is no text of its own
# What a run that landed nothing says of an article that holds its agents' earlier text: it stays as it was.
KEPT = "nothing of the run landed, so the article keeps what it held"
# ... and of one whose earlier text went whole, each line of it citing only facts withdrawn since
NONE_LEFT = "nothing of the run landed, and each line of its earlier text cited only facts withdrawn since, so the " \
    "article holds none of its text now"


def _withdrawn(grounding: _Grounding, store: Store, article: str, writers: list[str]) -> list[str]:
    """Take out of each part `writers` wrote on `article`, each standing from before as none of them rewrote it in
    this run, each line whose every citation names what was taken back since, as `_Grounding.withdrawn` reads it, and
    nothing else: the article stays as it was, but a claim whose fact was taken back does not stay for good. A part
    left with no text, only headings or nothing, goes. Gives a line for each line taken out. The text cites a fact by
    the label the store wrote, so each label is read back as its citation's id."""
    current = store.article(article)
    mine = [s for s in current.compilations if s.by in writers] if current is not None else []
    by_label = {label(c.id): c.id for s in mine for c in s.citations}

    def cited(text: str) -> list[str]:
        return [by_label.get(found, found) for found in CITE.findall(unmarked(text))]

    # a kept part lands citing only what its writer can cite now, so what it cites is read from its text too
    gone = grounding.withdrawn([*(c for s in mine for c in s.citations),
                                *(Citation("fact", id) for s in mine for id in cited(s.text))])
    if not gone:
        return []
    said: list[str] = []
    changed: list[tuple[Compilation, str]] = []
    for part in mine:
        kept: list[str] = []
        taken: list[str] = []
        for line in part.text.split("\n"):
            cites = cited(line)
            (taken if cites and all(id in gone for id in cites) else kept).append(line)
        said += [f"{part.by}, its earlier text: took out {line.strip()!r}: every fact it cites was withdrawn since"
                 for line in taken]
        if taken:
            changed.append((part, "\n".join(kept)))
    with store.exclusive():
        for part, text in changed:
            if any(line.strip() and not HEADING_LINE.match(line) for line in text.split("\n")):
                ids_left = set(cited(text))
                store.write_compilation(replace(part, text=text, citations=[c for c in part.citations if c.id in ids_left]))
            else:
                store.delete_compilation(article, part.heading)
    return said


def _has_word(line: str) -> bool:
    """Whether a line holds a letter or a digit past its leading quote and list marks: a lone > or an empty list item
    is no word."""
    rest = re.sub(r"^(?:\s*(?:>|[-*+](?=\s)|\d+[.)]))*", "", line.strip())
    return any(c.isalnum() for c in rest)


def no_sentence(text: str) -> bool:
    """Whether `text` holds no sentence: every line is blank, a heading, part of an HTML comment, or holds no word
    past its marks, and a table holds no row of words under its header."""
    lines = re.sub(r"<!--.*?(?:-->|$)", "", text, flags=re.DOTALL).splitlines()
    n = 0
    while n < len(lines):
        line = lines[n].strip()
        if line.startswith("|"):
            end = n
            while end < len(lines) and lines[end].strip().startswith("|"):
                end += 1
            rows = lines[n:end]
            body = rows[2:] if len(rows) >= 2 and not _has_word(rows[1]) and "-" in rows[1] else rows
            if any(_has_word(row) for row in body):
                return False
            n = end
            continue
        if not line.startswith("#") and _has_word(line):
            return False
        n += 1
    return True


def _cited(text: str, citable: dict[str, Citation]) -> list[Citation]:
    """The citations `text` cites, by the id a writer wrote or by the label the store wrote in its place."""
    by_label = {label(id): c for id, c in citable.items()}
    return [c for id in dict.fromkeys(CITE.findall(text)) if (c := citable.get(id) or by_label.get(id)) is not None]


def _heading(store: Store, heading: str) -> str:
    """A part's heading as the store writes it."""
    return land_heading(heading, store.scrubbing()).text


def birth(store: Store, agent: Agent, article: str, at: str) -> None:
    """Create the article a fixed agent writes, if it does not exist yet, titled from its path. A pass births it
    before the agent's first run, so the facts kept before it existed link to it and the run reads them."""
    if store.article(article) is not None:
        return
    folder, _, name = article.rpartition("/")
    try:
        store.create(Identity(id=article, collection=folder, title=name.replace("-", " ").capitalize(),
                              admitted_by=Caller(agent.id, "agent"), admitted_at=at))
    except StoreError:
        pass  # a curator or a source created it while the run was under way: land the compilations on it


class _Grounding:
    """How one compile grounds what its runs hand in: an editor for each run, on the decision model when the workspace
    has one, each call and each try of it held to the compilation cap, reading the line the run read for each
    citation, the facts linked to the article and every other fact a part cites, the day of the run in the
    workspace's zone, and what the workspace is about, from `workspace.md`. Every editor of the compile shares what
    was scored, so a passage one run handed in is not scored again for another."""

    def __init__(self, article: str, *, lookup: Lookup, root: Path, ledger: Ledger | None, clock: Clock,
                 decider: Decider | None, decision: ModelSpec | None, zone: tzinfo, floor: float):
        self.article, self.lookup, self.root, self.ledger = article, lookup, root, ledger
        self.clock, self.decider, self.decision, self.zone, self.floor = clock, decider, decision, zone, floor
        self.known: dict[tuple[str, str], list[Verdict]] = {}

    def linked(self) -> list[Fact]:
        """The current facts linked to the article, as `read` gives them."""
        return self.lookup.read(Caller("compile", "agent"), [self.article]).facts

    def withdrawn(self, citations: list[Citation]) -> set[str]:
        """Of what `citations` name, what was taken back since: a fact or a record a caller retracted, and each
        retracted fact of a record its source removed, which leaves the record no fact and no retract row. A fact of
        an earlier save, and one a curator, an agent or an app took back, are neither."""
        store = getattr(self.lookup, "store", None)
        rows = [p for p in proposals(store, "accepted") if p.kind in RETRACTS] if store else []
        by_caller = {p.target for p in rows if (p.decided_by or p.by).kind == "person"}
        named = {p.target for p in rows}
        gone = {c.id for c in citations if c.id in by_caller}
        ids = list(dict.fromkeys(c.id for c in citations if c.kind == "fact"))
        extraction = self.lookup.extraction
        if not ids or extraction is None:
            return gone
        out = [f for f in extraction.facts(ids=ids) if f.status == "retracted"]
        removed = {(f.source, f.record) for f in out if not {f.id, f.record} & named
                   and not extraction.facts(source=f.source, record=f.record, latest=False)}
        return gone | {f.id for f in out if f.record in by_caller or (f.source, f.record) in removed}

    def stale(self, facts: Mapping[str, Fact]) -> dict[str, str]:
        """The line a read shows under each of `facts` whose document a person marked out of date, or flagged, by id,
        so the judge is told what a reader is told."""
        if not facts:
            return {}
        ids = list(facts)
        return {id: line for start in range(0, len(ids), MAX_READ)
                for id, line in (getattr(self.lookup.read(Caller("compile", "agent"), ids[start:start + MAX_READ]),
                                         "stale", {}) or {}).items()}

    def editor(self, agent: Agent, reads: Callable[[], list[Evidence]]) -> Editor:
        """An editor for one run of `agent`, held to the agent file's `min_grounding` or the workspace's. `reads`
        gives what the run has read when the editor is asked, so what it read by its tools is found too."""
        decider, run = self.decider, new_run_id()
        if decider is not None and self.ledger is not None and self.decision is not None:
            decider = Held(decider, self.ledger, "compilation", self.decision, self.clock, run)
        judge = Judge(decider, self.article, _workspace(self.root), f"{self.clock().astimezone(self.zone):%Y-%m-%d}")

        def found(ids: list[str]) -> dict[str, dict[str, Any]]:
            entries = reads()
            citable = _citable(entries)
            facts = {f.id: f for f in self.linked()}
            wanted = [id for id in ids if id in citable and id not in facts]
            if wanted and self.lookup.extraction is not None:
                facts |= {f.id: f for f in self.lookup.extraction.facts(ids=wanted)}
            scrub = self.lookup.store.scrubbing() or (lambda text: text)
            texts = {c.id: scrub(item.text) for entry in entries for item in entry.items if item.text in entry.lines
                     for c in item.citations}
            cited = {id: facts[id] for id in ids if id in facts}
            stale, sides = self.stale(cited), own_sides(self.lookup.store.record, cited.values())
            return {id: fields_of(id, citable.get(id), cited.get(id), texts.get(id, ""), stale.get(id, ""),
                                  sides.get(cited[id].record) if id in cited else None) or {"text": ""}
                    for id in ids if id in citable or id in cited}

        floor = agent.min_grounding if agent.min_grounding is not None else self.floor
        return Editor(judge, found, floor, self.known, agent.output[0] if agent.role == "subagent" else "", run)


def _workspace(root: Path) -> str:
    """The body of the workspace's `workspace.md`, its front matter left out, or empty."""
    try:
        text = (root / "workspace.md").read_bytes().decode("utf-8", "replace").removeprefix("\ufeff")
    except OSError:
        return ""
    if text.startswith("---\n") and (end := text.find("\n---", 4)) != -1:
        text = text[end + 4:]
    return text.strip()


def _finish(store: Store, article_id: str, root: Path, compiled: Compiled, grounding: _Grounding) -> Compiled:
    article = store.article(article_id)
    if article is None:
        raise StoreError(f"article {article_id} was written and does not read back: check the store")
    compiled.article = article
    compiled.warning = _copy(store, article_id, article, root, grounding)
    return compiled


def _copy(store: Store, article_id: str, article: Article | None, root: Path, grounding: _Grounding) -> str:
    """Write `article_id`'s copy in `articles/`, or remember that it did not write, for the next pass to try again, and
    say so."""
    if article is None:
        return ""
    try:
        mirror(article, root, facts=grounding.linked(), zone=grounding.zone)
    except OSError as error:
        unmirrored(store, article_id, True)
        return (f"{article_id} is stored, and its copy articles/{article_id}.md was not written ({error.strerror or error}): fix "
                "the path, and the next pass writes it")
    unmirrored(store, article_id, False)
    return ""


UNMIRRORED = own_key("unmirrored")  # the articles whose copy in articles/ did not write, which each pass tries again


def unmirrored(store: Store, article: str, failed: bool) -> None:
    """Remember that `article`'s copy did not write, or forget it once one has."""
    if failed or article in (store.state(UNMIRRORED) or []):
        update_state(store, UNMIRRORED, lambda now: sorted(set(now or []) - {article} | ({article} if failed else set())))


def compile_article(agent: Agent, *, lookup: Lookup, store: Store, agent_run: AgentRun, root: Path,
                    functions: dict[str, Callable[..., Any]], agent_tools: dict[str, Callable[..., Any]],
                    workspace: Workspace | None = None, ledger: Ledger | None = None,
                    model: ModelSpec | None = None, clock: Clock = system_clock, this: str | None = None,
                    subagents: list[Agent] | None = None, scheduled: bool = False, decider: Decider | None = None,
                    decision: ModelSpec | None = None, zone: tzinfo = UTC,
                    starter: Starter | None = None, sources: Mapping[str, Connector] | None = None,
                    descriptions: Mapping[str, str] | None = None, only: str = "",
                    hold: Callable[[list[str]], str] | None = None, by_hand: bool = False,
                    min_grounding: float = FLOOR) -> Compiled:
    """Run `agent` on the article it writes, if anything it reads has moved, score what it hands in, and land it.

    `this` names the article, for an agent that writes many. `subagents` are the loaded agents a composer's names are
    looked up in; left out, a composer loads its subagents' files from beside it. `model` is what a run is priced at,
    unless the agent's file names its own. A `scheduled` compile runs even when nothing it reads has moved; one
    `by_hand`, a person's ask, runs only what moved, but at once after a failure. `only`, a subagent's id, runs that
    subagent of a composed article as `scheduled` would, and the agent that lists it after it. `hold`, serve's
    cooldown, is asked only when what moved is the evidence of the agents it is handed: a line that says why that
    rebuild waits, or empty to run it.

    With a `decider`, grounding and the keep judge of an `articles` line ask the decision model, priced on
    `decision` and held to the compilation cap. `min_grounding` is the workspace's floor, which an agent file's own
    takes the place of. `zone` is the workspace's: the day grounding reads claims against, the copy's dates, and the
    time a `left` line reads its query as of. `starter` runs a `script` line, `sources` are the connectors a `sql`
    line reads, and `descriptions` give each collection the line a `rendered:` part shows of it.
    """
    _refuse_role(agent)
    article = _article(agent, this)
    current = store.article(article)
    if why := out_of_working_set(article, current.identity if current is not None else None):
        # no agent runs on an article out of the working set, however its run started: nothing is charged
        return Compiled(None, current, skipped=True, waiting=why, left_alone=why)
    grounding = _Grounding(article, lookup=lookup, root=root, ledger=ledger, clock=clock, decider=decider,
                           decision=decision, zone=zone, floor=min_grounding)
    c = _Compile(lookup=lookup, store=store, agent_run=agent_run, root=root, functions=functions,
                 agent_tools=agent_tools, workspace=workspace, ledger=ledger, model=model, clock=clock, grounding=grounding,
                 scheduled=scheduled, hold=hold, by_hand=by_hand, starter=starter, zone=zone, sources=sources,
                 judge=keep_judge(decider, store, clock, ledger, decision, root))
    if agent.role == "composer":
        return _Composing(c, agent, article, _subagents(agent, subagents), subagents, only).compose()
    return _single(c, agent, article, descriptions, subagents)


def keep_judge(decider: Decider | None, store: Store, clock: Clock, ledger: Ledger | None, decision: ModelSpec | None,
               root: Path) -> DecisionKeepJudge | None:
    """The judge a run's `keep` lines are cut by: the decision model's, with its scores kept in `store`, held to the
    compilation cap in `ledger` and priced on `decision`, or None with no decision model."""
    if decider is None:
        return None
    return DecisionKeepJudge(decider, store, clock, ledger, decision, root)


@dataclass
class _Compile:
    """What every run of one compile shares: what it reads and writes through, how a run is run and grounded, and
    what makes a run due."""

    lookup: Lookup
    store: Store
    agent_run: AgentRun
    root: Path
    functions: dict[str, Callable[..., Any]]
    agent_tools: dict[str, Callable[..., Any]]
    workspace: Workspace | None
    ledger: Ledger | None
    model: ModelSpec | None
    clock: Clock
    grounding: _Grounding
    scheduled: bool
    hold: Callable[[list[str]], str] | None
    by_hand: bool
    starter: Starter | None  # what runs a `script` line
    zone: tzinfo
    sources: Mapping[str, Connector] | None  # the connectors a `sql` line reads
    judge: DecisionKeepJudge | None  # what cuts an `articles` line with `keep`

    def gather(self, agent: Agent, article: str, writers: list[str], now: datetime) -> list[Evidence]:
        """What a run reads: the agent's evidence on `article`, then what people left for it, which counts in its digest
        like any evidence. A gather that fails counts as a failed run, so it raises `_GatherWaits` while it waits."""
        return _gather(agent, article, self.store, self.model, self.clock, self.scheduled, self.lookup, self.functions,
                       self.workspace, this=article, writers=writers, root=self.root, now=now,
                       last=_last_sets(self.store, agent, article), starter=self.starter, zone=self.zone,
                       sources=self.sources, judge=self.judge) + _inbox(self.store, agent, article)

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], editor: Editor) -> Run:
        """`run_agent` under this compile's caps and in its log, priced on the model the agent's own file names, if it
        names one, else on the compile's, its first hand-in shown to `editor`."""
        return run_agent(agent, evidence, tools, agent_run=self.agent_run, store=self.store, ledger=self.ledger,
                         model=_model(agent, self.model), clock=self.clock, review=editor.review, run_id=editor.run)


def _single(c: _Compile, agent: Agent, article: str, descriptions: Mapping[str, str] | None,
            others: list[Agent] | None) -> Compiled:
    """An agent that writes its article alone: code writes what `rendered:` names, and the agent runs when what it
    reads moved, unless it cools down or waits after a failure."""
    store, clock = c.store, c.clock
    try:
        evidence = c.gather(agent, article, [agent.id], clock())
    except _GatherWaits as why:
        return Compiled(None, store.article(article), skipped=True, waiting=str(why))
    stamp_ = digest(agent, evidence + _read_before(c.lookup, store, agent, article))
    current = store.article(article)
    if current is None and not fixed(agent):
        raise CompileError(f"{_name(agent)} writes articles/{article}.md, which does not exist: its collection's curator "
                           "admits it, so compile one of the articles targets lists")
    owned = current is not None and any(s.by == agent.id for s in current.compilations)
    status, why = due(agent, article, store, stamp_, clock(), fresh=unmoved(store, agent, article, stamp_),
                      scheduled=c.scheduled, by_hand=c.by_hand, hold=c.hold)
    if status == "cooling down":
        return Compiled(None, current, skipped=True, waiting=why)  # what rendered: names, the pass's rendering writes
    drew = rendered.land(store, agent, article, stamp(clock()), descriptions)  # code writes what rendered: names, no model
    current = store.article(article)
    if status == "fresh":
        if drew:
            unmirrored(store, article, True)  # the pass writes its copy
        return Compiled(None, current, skipped=True, rendered=drew)
    if status == "waiting":
        return Compiled(None, current, skipped=True, waiting=why)
    return _write(c, agent, article, evidence, owned, bool(drew), others)


def _lands(run: Run, stands: bool) -> bool:
    """Whether what a run handed in and the fields it set land: it ran to its end, or its budget ran out and some of
    its text `stands` after grounding."""
    return run.status in ("done", WROTE_NOTHING) or run.status == "budget spent" and stands


def _write(c: _Compile, agent: Agent, article: str, evidence: list[Evidence], owned: bool, drew: bool,
           others: list[Agent] | None) -> Compiled:
    """Run the agent and land what stands of what it handed in, as one part written whole. When nothing does, the
    article stays as it was, less each line whose every cited fact was withdrawn."""
    store, clock, grounding = c.store, c.clock, c.grounding
    writes = _Writes(article, store, agent.id)
    seen: list[Fact | Article] = []
    tools = _tools(agent, c.lookup, c.agent_tools, c.workspace, seen) + writes.tools()
    editor = grounding.editor(agent, lambda: [*evidence, *_by_tool(seen), *_articles_read(seen)])
    run = c.run(replace(agent, target=article), evidence, tools, editor)
    evidence = evidence + _by_tool(seen)  # what it read by tool, it read: it may cite it, and its digest counts it
    stamp_ = digest(agent, evidence)
    store.set_state(_tool_reads(agent, article), sorted({f.id for f in seen if isinstance(f, Fact)}))
    _tried(store, agent, article, stamp_, clock(), run.status not in ("done", WROTE_NOTHING, "budget spent"))
    citable = _citable(evidence, _articles_read(seen))
    text, dropped = _landing(editor, agent, run.result.text) if run.result else ("", [])
    if run.status in ("done", WROTE_NOTHING) and not text:
        _wrote_nothing(store, run, dropped)
    ended = run.status in ("done", WROTE_NOTHING)

    def read() -> None:
        if ended:  # read, whatever lands: the same evidence does not run it again
            store.set_state(_read(agent, article),
                            _land_own(c, article, [agent.id], agent, evidence, writes, run.ended_at) or stamp_)

    lands = _lands(run, bool(text))  # land what it handed in
    if lands and text:
        _scored(store, run, editor)
    if not lands or not text:
        read()  # what it set lands, whatever its text
        notes: list[str] = []
        if ended:  # and a retraction still tells
            gone = _withdrawn(grounding, store, article, [agent.id])
            dropped += gone
            if owned:
                after_ = store.article(article)
                notes.append(KEPT if after_ is not None and any(s.by == agent.id for s in after_.compilations)
                             else NONE_LEFT)
        if drew or ended and (gone or writes.sets):  # what changed the article, so its copy follows
            _copy(store, article, store.article(article), c.root, grounding)
        return Compiled(run, None, dropped=dropped, runs=[run], notes=notes)
    if fixed(agent):
        birth(store, agent, article, run.started_at)
    parts = [_Part(agent.id, agent.id, text, citable, run.started_at, run.ended_at, whole=True)]
    orphans = _orphans(agent, article, store, others)

    def after() -> None:
        read()
        if not ended:  # a run cut short lands what it set, and is not done reading: the next pass runs it
            writes.land(run.ended_at)
        elif (seen := store.state(_read(agent, article))) != stamp_:  # its part read what its own writes moved too
            store.write_compilation(replace(next(s for s in store.article(article).compilations if s.by == agent.id),
                                            digest=seen))
        _drop_material_of(store, article, orphans)
        _keep_left(store, agent, article, evidence, run.started_at)
        rendered.place(store, agent, article)

    finished = stamp_ if ended else ""  # a run cut short is not done reading: the next pass runs it
    try:
        again, noted = _land(store, article, parts, finished, [agent.id, *orphans], after)
    except _LeftAlone as left:
        return Compiled(run, store.article(article), skipped=True, waiting=str(left), left_alone=str(left), runs=[run])
    short = [] if run.status == "done" else [f"its run {run.status} ({run.note})"]
    short += [line for line in run.note.split("; ") if line.startswith(SUMMARY_NOTE)] if run.status == "done" else []
    return _finish(store, article, c.root, Compiled(run, None, dropped=dropped, runs=[run], notes=[*short, *noted],
                                                    cleaned=again), grounding)


def _land_own(c: _Compile, article: str, writers: list[str], agent: Agent, evidence: list[Evidence], writes: _Writes,
              at: str) -> str | None:
    """Land the fields one agent's run set, at when it ended, and give the digest of what it has read: what it read,
    with what its own writes alone moved in it taken in, measured as a curator's acts are. None when it set none."""
    from .curate import _Measure, store_lines  # curate runs its agents through this module

    if not writes.sets:
        return None
    measure = _Measure(c.store, agent, partial(gather, store_lines(agent, c.functions), c.lookup, {}, c.workspace,
                                               this=article, writers=writers, root=c.root, now=c.clock(), zone=c.zone,
                                               last=_last_sets(c.store, agent, article)),
                       getattr(c.lookup, "extraction", None))
    measure.stored = fingerprint(agent, evidence)
    measure(partial(writes.land, at))
    return digest_of(measure.stored)


FAILED_WAIT = 30  # seconds after a first failed run, one pass under serve's default: each failure on the same
FAILED_WAIT_MOST = 3600  # evidence doubles it, up to this


def _failing(agent: Agent, article: str) -> str:
    """The state key that keeps how often an agent's run on an article failed on the same evidence, and when last."""
    return f"failing:{agent.id}:{article}"


def unmoved(store: Store, agent: Agent, article: str, stamp_: str) -> bool:
    """Whether nothing one agent's run on `article` reads, digest `stamp_`, moved since it last read it or wrote
    there."""
    current = store.article(article)
    owned = [s for s in current.compilations if s.by == agent.id] if current is not None else []
    return store.state(_read(agent, article)) == stamp_ or bool(owned) and all(s.digest == stamp_ for s in owned)


def due(agent: Agent, article: str, store: Store, stamp_: str, now: datetime, *, fresh: bool,
        scheduled: bool = False, by_hand: bool = False,
        hold: Callable[[list[str]], str] | None = None) -> tuple[str, str]:
    """What compile does now with one agent's run on `article`, on evidence of digest `stamp_`: ("", "") runs
    it, else the status it stands at and why, "fresh", "cooling down" or "waiting". `fresh` says nothing it reads
    moved since its last run. A `scheduled` run runs though it is fresh. A run `by_hand`, a person's ask, runs though
    it failed on the same evidence. `hold` is the cooldown, which only a pass hands in. `compile --dry-run` asks this
    too, so what it says is what compile does."""
    if fresh and not scheduled:
        return "fresh", ""
    if not scheduled and hold is not None and (why := hold([agent.id])):
        return "cooling down", why
    if not scheduled and not by_hand and (why := _backs_off(store, agent, article, stamp_, now)):
        return "waiting", why
    return "", ""


def _until(failing: Mapping[str, Any]) -> datetime:
    """When an agent that failed `times` in a row, the last `at`, may run again: `FAILED_WAIT` doubling each time."""
    times = int(failing.get("times", 1))
    return datetime.fromisoformat(failing["at"]) + timedelta(seconds=min(FAILED_WAIT * 2 ** (times - 1),
                                                                         FAILED_WAIT_MOST))


def _backs_off(store: Store, agent: Agent, article: str, stamp_: str, now: datetime) -> str:
    """Why an agent whose run failed on this same evidence waits before it is paid for again, or empty."""
    kept = store.state(_failing(agent, article))
    if not kept or kept.get("stamp") != stamp_:
        return ""
    times = int(kept.get("times", 1))
    until = _until(kept)
    if now >= until:
        return ""
    return (f"its run failed {times} {'time' if times == 1 else 'times'} on the same evidence, so it waits until "
            f"{stamp(until)} before it runs again, or until its evidence moves")


def _tried(store: Store, agent: Agent, article: str, stamp_: str, now: datetime, failed: bool) -> None:
    """Count a failed run on its evidence, or forget the count once a run did not fail."""
    kept = store.state(_failing(agent, article))
    if not failed:
        if kept is not None:
            store.set_state(_failing(agent, article), None)
        return
    counts = kept and (kept.get("stamp") == stamp_ or kept.get("gather") and kept.get("stamp") in (None, stamp_))
    times = int(kept.get("times", 1)) + 1 if counts else 1  # a failed gather before it counts
    store.set_state(_failing(agent, article), {"stamp": stamp_, "times": times, "at": stamp(now)})


class _Ended(AgentRun):
    """Hands back a run that ended before any model was asked, so `run_agent` keeps it as it keeps every run."""

    def __init__(self, ended: Run):
        self.ended = ended

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Any) -> Run:
        return self.ended


class _GatherWaits(Exception):
    """Why an agent whose evidence failed to gather waits before it is gathered again."""


def _inputs(agent: Agent, root: Path) -> str:
    """A digest of what a person edits that a run reads: the agent's file and `workspace.md`. A file that does not
    read hashes as empty."""
    hashed = hashlib.sha256()
    for path in (agent.path, root / "workspace.md"):
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        hashed.update(f"{len(data)}\n".encode() + data)
    return "inputs:" + hashed.hexdigest()[:16]


def _gather(agent: Agent, article: str, store: Store, model: ModelSpec | None, clock: Clock, scheduled: bool,
            *args: Any, **kwargs: Any) -> list[Evidence]:
    """`gather`, where one that fails counts as a failed run: the agent waits as after one before it gathers again,
    unless its file or `workspace.md` changes or the run is `scheduled`, and `agent-runs` keeps why once while it
    fails in a row, with no model asked. A gather that works leaves the count standing: only a run that works ends
    it, or a run on evidence that moved."""
    inputs = _inputs(agent, kwargs["root"])
    kept = store.state(_failing(agent, article)) or {}
    again = bool(kept.get("gather")) and kept.get("inputs") == inputs  # it failed to gather last, on these inputs
    if again and not scheduled:
        times = int(kept.get("times", 1))
        until = _until(kept)
        if clock() < until:
            raise _GatherWaits(f"its evidence failed to gather, the last of {times} "
                               f"{'failure' if times == 1 else 'failures'} in a row, so it waits until {stamp(until)} "
                               "before it gathers again, or until its agent file or workspace.md changes")
    try:
        return gather(agent, *args, **kwargs)
    except BudgetSpent:
        raise
    except Exception as error:
        note, now = GATHER + str(error), clock()
        counts = kept and (not kept.get("gather") or again)  # a failed run before it, or a gather that failed so
        times = int(kept.get("times", 1)) + 1 if counts else 1
        store.set_state(_failing(agent, article), {**({"stamp": kept["stamp"]} if counts and "stamp" in kept else {}),
                                                "inputs": inputs, "times": times, "at": stamp(now), "gather": True})
        if not kept.get("gather"):  # the first failed gather in a row: its row says why
            ended = Run("", agent.id, article, "failed", started_at=stamp(now), ended_at=stamp(now), note=note)
            run_agent(replace(agent, target=article), [], [], agent_run=_Ended(ended), store=store,
                      model=_model(agent, model), clock=clock)
        raise


def _kept_note(store: Store, note: str) -> str:
    """A run's note as the `agent-runs` log keeps it: masked as the store masks a row, then cut to 300 characters. A
    cut first could leave part of a secret, which no mask finds."""
    scrub = store.scrubbing()
    return _cut(masked(note, scrub) if scrub is not None else note, None, 300)


def _run_row(store: Store, run: Run) -> dict[str, Any] | None:
    """The latest row the `agent-runs` log holds of `run`."""
    return next((row.data for row in reversed(store.rows("agent-runs", {"agent": run.agent}))
                 if row.data.get("run") == run.id), None)


def _wrote_nothing(store: Store, run: Run, dropped: list[str]) -> None:
    """Mark a run that ended with nothing to land as `wrote nothing`, with why, and say so in a later row of the
    `agent-runs` log, whose latest row for an agent is what `status` shows."""
    if run.status != WROTE_NOTHING:
        run.status = WROTE_NOTHING
        why = f"nothing it handed in lands: {dropped[0]}" if dropped else "it handed in nothing"
        run.note = f"{run.note}; {why}" if run.note else why
    if (first := _run_row(store, run)) is not None:
        store.append("agent-runs", {**first, "status": WROTE_NOTHING, "note": _kept_note(store, run.note)})


NOTES_MOST = 16_000  # characters of what grounding said that one run's line keeps, as `_kept_note` bounds a note


def keep_grounding(store: Store, run: Run | None, tally: Mapping[str, int], lines: list[str]) -> None:
    """Keep what grounding did on the run's line of the `agent-runs` log: a later row of the same run, as it stood,
    with `grounding`, the counts by kind, when there are any, and `notes`, the lines it held already, then each line
    of what grounding scored and of what landing made safe, whole and masked as the store masks a row. One run keeps
    at most `NOTES_MOST` characters of them, whole lines in order, and a last line counts the rest, so the log grows by
    a bounded row a run."""
    if run is None or not run.id or not (tally or lines):
        return
    if (last := _run_row(store, run)) is None:
        return
    scrub = store.scrubbing()
    kept: list[str] = list(last.get("notes") or [])
    room = NOTES_MOST - sum(len(line) for line in kept)
    for n, line in enumerate(lines):
        line = scrub(line) if scrub is not None else line  # masked as the store masks a row, never cut
        if len(line) > room:
            kept.append(f"and {len(lines) - n} more, not kept")
            break
        kept.append(line)
        room -= len(line)
    store.append("agent-runs", {**last, "article": run.target, **({"grounding": dict(tally)} if tally else {}),
                                "notes": kept})


def _scored(store: Store, run: Run, editor: Editor) -> None:
    """Keep each sentence `editor` scored of a run's text on its run's row, and count what it marked and left unscored
    toward the article compiling."""
    tallied(editor.tally)
    keep_grounding(store, run, {}, editor.scores)


# A subagent's run that failed fails the article's whole run, and nothing of it lands.
SUB_FAILED = "a subagent's run failed, so nothing of the article's run lands"
# What a composed article holds when none of its subagents' content stood, nor anything its agent handed in.
NONE_STAND = "nothing of its subagents' content stood, so the article holds none of their text now"


def _no_model_row(store: Store, agent: Agent, article: str, status: str, at: str, note: str) -> None:
    """A row in the `agent-runs` log for an article's run that asked no model, which `cerebro status` shows."""
    store.append("agent-runs", {"run": "", "agent": agent.id, "target": article, "status": status, "provider": "",
                                "model": "no model", "requests": 0, "tool_calls": 0, "ended_at": at, "note": note})


def _sub_failed(store: Store, composer: Agent, article: str, failed: list[tuple[Agent, Run]], compiled: Compiled,
                clock: Clock) -> Compiled:
    """The article's run as a whole, failed since a subagent's run failed: nothing of it lands, and the agent itself
    does not run. Its line gives each failed run, and a row in the `agent-runs` log says so of the agent."""
    why = "; ".join(f"{sub.id}: run {run.id} {run.status}" + (f": {run.note}" if run.note else "")
                    for sub, run in failed)
    at = stamp(clock())
    run = Run("", composer.id, article, "failed", note=f"{SUB_FAILED}: {why}",
              started_at=min((r.started_at for _, r in failed if r.started_at), default=at), ended_at=at)
    _no_model_row(store, composer, article, "failed", at, run.note[:300])
    compiled.run = run
    return compiled


def _subagents(composer: Agent, given: list[Agent] | None) -> list[Agent]:
    """The composer's subagents, in the order `subagents:` lists them."""
    by_id = {agent.id: agent for agent in given or []}
    out = []
    for sub_id in composer.subagent_ids:
        if sub_id in by_id:
            out.append(by_id[sub_id])
        elif given is not None:
            raise CompileError(f"{_name(composer)} lists the subagent {sub_id.rsplit('/', 1)[1]}, and agents/{sub_id}.md"
                               " did not load: fix that file, as its own line says, and compile again")
        else:
            agents_dir = composer.path.parent.parent
            out.append(load_agent(agents_dir / f"{sub_id}.md", agents_dir))
    return out


def _part_of(sub: Agent, composer: Agent, article: str, root: Path) -> Agent:
    """A subagent as it runs inside its composer's run: on the article, with what the composer says to every part."""
    words = sub.prompt
    if composer.context:
        words = prompts.fill("subagent", root, context=composer.context) + "\n\n" + words
    return replace(sub, target=article, prompt=words)


def material_key(article: str) -> str:
    """The state key that keeps what each subagent last returned on an article, by its id: material for the agent that
    lists it, model-written text that is never shown, read, searched or logged."""
    return f"material:{article}"


def drop_material(store: Store, *article_ids: str) -> None:
    """Let go of what the subagents returned on each of `article_ids`, as when the article is archived, merged or
    split."""
    for article in article_ids:
        if store.state(material_key(article)) is not None:
            store.set_state(material_key(article), None)


def _keep_material(store: Store, article_id: str, subs: list[Agent], held: list[dict[str, Any]], landed: bool) -> None:
    """Keep what each subagent returned on `article_id`, marked landed or not, unless the article left the working set
    while it compiled: what its subagents returned goes with it."""
    with store.exclusive():
        article = store.article(article_id)
        if article is not None and not in_working_set(article.identity):
            return
        kept = {sub.id: {k: v for k, v in h.items() if not (landed and k == "landed")}
                for sub, h in zip(subs, held) if h}
        store.set_state(material_key(article_id), kept or None)


def _drop_material_of(store: Store, article: str, gone: list[str]) -> None:
    """Let go of what each subagent of `gone` returned on `article`."""
    kept = store.state(material_key(article)) or {}
    if set(kept) & set(gone):
        left = {id: held for id, held in kept.items() if id not in gone}
        store.set_state(material_key(article), left or None)


def _orphans(agent: Agent, article: str, store: Store, others: list[Agent] | None) -> list[str]:
    """The subagents that wrote a part of `article`, or keep material on it, that neither `agent` nor any other agent
    composing `article` lists any more: one renamed, taken off `subagents:`, or of an agent that writes it whole now."""
    current = store.article(article)
    found = [s.by for s in (current.compilations if current is not None else [])]
    found = [id for id in dict.fromkeys([*found, *(store.state(material_key(article)) or {})])
             if id.count("/") == 2 and id.split("/")[1] == "subagents" and id not in agent.subagent_ids]
    if not found:
        return []
    loaded = others if others is not None else load_agents(agent.path.parent.parent)[0]
    listed = {sub for other in loaded if other.id != agent.id and other.role == "composer"
              and (other.target == article if fixed(other) else article.split("/")[0] == other.target)
              for sub in other.subagent_ids}
    return [id for id in found if id not in listed]


def _drafts(subs: list[Agent], kept: list[dict[str, Any]]) -> Evidence:
    """What every subagent returned, under its heading with the subagent that returned it. It cites nothing of its
    own."""
    lines = [f"### {sub.output[0]}, from {sub.id}\n\n{held['text'].strip()}"
             for sub, held in zip(subs, kept) if held.get("text", "").strip()]
    return Evidence(DRAFTS, lines or ["No subagent returned anything."], spelled="drafts")


def _returned(subs: list[Agent], held: list[dict[str, Any]]) -> list[str]:
    """What each subagent last returned, as an instructed agent reads it: a subagent that runs again and returns the
    same moves nothing."""
    return [f"{sub.id}\0{h.get('text', '')}" for sub, h in zip(subs, held)]


def _not_run(agent: Agent, article: str, error: BudgetSpent | Stopped, clock: Clock) -> Run:
    """A run refused before it started, with why: held for room under the cap, the cap spent, or serve stopping."""
    status = STOPPED if isinstance(error, Stopped) else "held" if isinstance(error, HeldForRoom) else "budget spent"
    return Run("", agent.id, article, status, note=str(error), started_at=stamp(clock()), ended_at=stamp(clock()))


def _stamp_of(*digests: str) -> str:
    return "sha256:" + hashlib.sha256("\0".join(digests).encode()).hexdigest()[:16]


class _Composing:
    """One run of an agent with subagents on one article, a step a method: `due` gathers what each reads and says
    whether anything moved, `draft` runs each stale subagent side by side and keeps what it returned as material,
    `run_own` runs the agent itself on every subagent's content, `plan_own`, `join` and `settle` decide which parts
    land, and `land` writes them, or keeps the article as it was.

    A subagent's run that fails fails the article's whole run, and so does the agent's own: nothing lands, and what
    the subagents returned is kept, not landed. While a subagent waits after its failure, the article's run waits
    with it.

    Every piece of state is declared in `__init__`, under the step that fills it; a step reads only what the steps
    before it filled. Lists by subagent follow `subagents:` order, and dicts by subagent are keyed by that index."""

    def __init__(self, c: _Compile, composer: Agent, article: str, subs: list[Agent], others: list[Agent] | None,
                 only: str):
        self.c, self.composer, self.article, self.subs, self.others, self.only = c, composer, article, subs, others, only
        self.writers = [composer.id, *(sub.id for sub in subs)]
        self.instructed = bool(composer.own)  # the agent has instructions of its own, so it runs on the drafts
        # due
        self.now = datetime.min  # when the compile started, read once for every subagent
        self.lead: list[Evidence] = []  # what the agent itself reads, with instructions of its own
        self.parts: list[list[Evidence]] = []  # what each subagent reads, and later what its run read by its tools
        self.before: list[list[Evidence]] = []  # what each subagent's last run read by its tools
        self.stamps: list[str] = []  # the digest of what each subagent reads
        self.held: list[dict[str, Any]] = []  # what each subagent last returned, with when and on what digest
        self.current: Article | None = None  # the article as the compile found it
        self.own_read = ""  # the digest of what the agent itself reads
        self.stale: list[int] = []  # the subagents that run
        self.orphans: list[str] = []  # subagents no agent lists any more, whose parts and material go
        # draft
        self.own_seen: list[Fact | Article] = []  # what the agent's own run reads by its tools
        self.own_tools: list[Callable[..., Any]] = []
        self.sub_writes: dict[int, _Writes] = {}  # the fields each subagent's run sets
        self.editors: dict[int, Editor] = {}  # what scores each subagent's run
        self.sub_seen: dict[int, list[Fact | Article]] = {}  # what each subagent's run read by its tools
        self.holds: dict[int, BudgetSpent] = {}  # the subagents refused under the cap before they ran
        self.rows: dict[str, Compilation] = {}  # each subagent's part on the article, by its id
        self.ran: dict[int, Run] = {}  # each subagent's run
        self.failed: list[int] = []  # the subagents whose run failed
        self.kept_parts: dict[int, str] = {}  # what a subagent that returned nothing keeps of what it wrote last
        self.kept_from: dict[int, int] = {}  # the index in `compiled.notes` of that kept part's note
        self.compiled = Compiled(None, None)  # what the compile gives back, filled as it goes
        # run_own
        self.citable: list[dict[str, Citation]] = []  # what each subagent's part may cite
        self.reads: list[list[Evidence]] = []  # what each subagent read, this run or its last
        self.everything: dict[str, Citation] = {}  # what the agent's own text may cite
        self.writes = _Writes(article, c.store, composer.id)  # the fields the agent's own run sets
        self.own: Run | None = None  # the agent's own run, when it ran
        self.editor = c.grounding.editor(composer, lambda: [  # what scores the agent's own run, on all it read
            *self.lead, *_by_tool(self.own_seen), *_articles_read(self.own_seen), *(e for r in self.reads for e in r),
            *_articles_read([a for seen in self.sub_seen.values() for a in seen])])
        # plan_own, join and settle
        self.plan: list[_Part] = []  # the parts that land
        self.drafts: set[int] = set()  # drafts returned since the agent last composed, never landed
        self.stood: set[str] = set()  # the agents whose text stands as grounded on this pass
        # `new`: some text lands, this run's or what a subagent returned and never landed; `composed`: what the agent
        # composed last stands, less what was withdrawn; `leave`: the agent's text stands as it is; `redrafted`: a
        # draft returned since the agent last composed lands
        self.leave = self.unstood = self.new = self.composed = self.drafted = self.redrafted = False

    def compose(self) -> Compiled:
        if (skipped := self.due()) is not None:
            return skipped
        if (failed := self.draft()) is not None:
            return failed
        if (ended := self.run_own()) is not None:
            return ended
        self.plan_own()
        if not self.plan and not self.leave and not self.composed:
            self.join()
        self.settle()
        return self.land()

    def due(self) -> Compiled | None:
        """Gather what the agent and each subagent read: a skip when nothing moved, or when a run waits."""
        c, store, article, subs, composer = self.c, self.c.store, self.article, self.subs, self.composer
        self.now = c.clock()
        try:
            if self.instructed:
                self.lead = c.gather(composer, article, self.writers, self.now)
            self.parts = [c.gather(sub, article, self.writers, self.now) for sub in subs]
        except _GatherWaits as why:
            return Compiled(None, store.article(article), skipped=True, waiting=str(why))
        self.before = [_read_before(c.lookup, store, sub, article) for sub in subs]
        self.stamps = [_stamp_of(digest(sub, read + by), composer.context)
                       for sub, read, by in zip(subs, self.parts, self.before)]
        kept = store.state(material_key(article)) or {}
        self.held = [dict(kept.get(sub.id) or {}) for sub in subs]
        self.current = store.article(article)
        if self.current is None and not fixed(composer):
            raise CompileError(f"{_name(composer)} composes articles/{article}.md, which does not exist: its collection's "
                               "curator admits it, so compile one of the articles targets lists")
        self.own_read = digest(composer, self.lead + _read_before(c.lookup, store, composer, article))
        own_stamp = _stamp_of(self.own_read, *_returned(subs, self.held))
        stale = [n for n, sub in enumerate(subs) if (sub.id == self.only if self.only else c.scheduled or
                                                     self.held[n].get("digest") != self.stamps[n])]
        due = self.instructed and (c.scheduled and not self.only or store.state(_read(composer, article)) != own_stamp)
        waits = not c.scheduled and not c.by_hand
        backing = {n: why for n in stale
                   if waits and (why := _backs_off(store, subs[n], article, self.stamps[n], self.now))}
        self.stale = [n for n in stale if n not in backing]
        if backing:  # a subagent's run failed on this evidence, and the article's run waits with it as a whole
            return Compiled(None, self.current, skipped=True, waiting=next(iter(backing.values())))
        if waits and not self.stale and due and (why := _backs_off(store, composer, article, own_stamp, self.now)):
            return Compiled(None, self.current, skipped=True, waiting=why)  # its own run failed on this: it waits
        order = [s.by for s in (self.current.compilations if self.current is not None else []) if s.by in self.writers]
        joined = [sub.id for sub in subs if sub.id in order]
        self.orphans = _orphans(composer, article, store, self.others)
        unlanded = any(h.get("landed") is False for h in self.held)  # returned on a pass that failed before it landed
        if not self.stale and not due and (self.instructed or order == joined) and not self.orphans and not unlanded:
            return Compiled(None, self.current, skipped=True)
        moved = [subs[n].id for n in self.stale] + ([composer.id] if due else [])
        if not c.scheduled and c.hold is not None and moved and not self.orphans and not unlanded and (
                self.instructed or order == joined) and (why := c.hold(moved)):
            return Compiled(None, self.current, skipped=True, waiting=why)
        return None

    def draft(self) -> Compiled | None:
        """Run each stale subagent side by side, and keep what each returned as material, not landed yet: the
        article's run failed when one of them failed. A stop of serve is raised once what the others returned is
        kept."""
        c, store, article, subs = self.c, self.c.store, self.article, self.subs
        seen = self.sub_seen = {n: [] for n in self.stale}
        tools = {n: _tools(subs[n], c.lookup, c.agent_tools, c.workspace, seen[n]) for n in self.stale}  # refused first
        self.own_tools = _tools(self.composer, c.lookup, c.agent_tools, c.workspace, self.own_seen)
        self.sub_writes = {n: _Writes(article, store, subs[n].id) for n in self.stale}
        self.editors = editors = {n: c.grounding.editor(subs[n], partial(self.read_by, n, seen[n]))
                                  for n in self.stale}
        stops: list[Stopped] = []

        def one(n: int) -> Run:
            try:
                return c.run(_part_of(subs[n], self.composer, article, c.root), self.parts[n],
                             tools[n] + self.sub_writes[n].tools(), editors[n])
            except BudgetSpent as error:  # refused before it ran: it stands aside, and the others land
                self.holds[n] = error
                return _not_run(subs[n], article, error, c.clock)
            except Stopped as error:  # the others' content is kept below, so a stop pays for no finished draft twice
                stops.append(error)
                return _not_run(subs[n], article, error, c.clock)

        self.rows = {s.by: s for s in (self.current.compilations if self.current is not None else []) if not s.whole}
        with ThreadPoolExecutor(max_workers=max(1, len(self.stale))) as pool:
            futures = {n: pool.submit(contextvars.copy_context().run, one, n) for n in self.stale}
        self.ran = {n: future.result() for n, future in futures.items()}
        if self.holds and len(self.holds) == len(self.ran) and not self.orphans:
            raise next(iter(self.holds.values()))  # nothing ran, so nothing was paid for: the pass holds it as a whole
        self.compiled = Compiled(None, None, runs=[run for n, run in self.ran.items() if n not in self.holds])
        self.failed = [n for n, run in self.ran.items() if run.status not in ("done", WROTE_NOTHING)
                       and not (run.status == "budget spent" and run.result)]
        self.compiled.notes += [line for n, run in self.ran.items() if n not in self.failed
                                for line in run.note.split("; ") if line.startswith(SUMMARY_NOTE)]
        for n in self.failed:
            if n in self.holds:
                self.compiled.notes.append(f"{subs[n].id}: held for room under the cap ({self.ran[n].note}), so what "
                                           "it returned before stands, and it runs when there is room")
        for n, run in self.ran.items():
            if n not in self.holds:
                self.returned(n, run, seen[n], editors[n])
        if broke := [n for n in self.failed if n not in self.holds]:
            return _sub_failed(store, self.composer, article, [(subs[n], self.ran[n]) for n in broke], self.compiled,
                               c.clock)
        if any(n not in self.failed for n in self.ran):  # kept, not landed: a pass that fails from here lands it
            _keep_material(store, article, subs, self.held, landed=False)  # unpaid next time
        if stops:
            raise stops[0]
        return None

    def read_by(self, n: int, seen: list[Fact | Article]) -> list[Evidence]:
        """What subagent `n` has read so far: its evidence, and what it read by its tools."""
        return [*self.parts[n], *_by_tool(seen), *_articles_read(seen)]

    def returned(self, n: int, run: Run, seen: list[Fact | Article], editor: Editor) -> None:
        """What subagent `n`'s run read and returned, as it lands, kept in `held`, and what its editor scored of it on
        its run's row. One that returned nothing keeps what it wrote last."""
        c, store, article, sub, held = self.c, self.c.store, self.article, self.subs[n], self.held
        self.parts[n] = self.parts[n] + _by_tool(seen)
        self.stamps[n] = _stamp_of(digest(sub, self.parts[n]), self.composer.context)
        store.set_state(_tool_reads(sub, article), sorted({f.id for f in seen if isinstance(f, Fact)}))
        failed = n in self.failed
        _tried(store, sub, article, self.stamps[n], c.clock(), failed and run.status not in ("budget spent", STOPPED))
        if failed:
            return
        text = editor.marked(run.result.text).strip() if run.result else "" if run.status == WROTE_NOTHING else \
            held[n].get("text", "")
        row = self.rows.get(sub.id) if not self.instructed else None
        if row is not None:
            last = unlabeled(row.text, row.citations)
        else:
            last = held[n].get("text", "") if self.instructed else ""
        if no_sentence(text) and last.strip() and run.status != "budget spent":  # none returned: it keeps its last
            text = last
            self.kept_from[n] = len(self.compiled.notes)
            self.compiled.notes.append(f"{sub.output[0]}: empty, {KEPT_LAST}")
            self.kept_parts[n] = text
        written = (held[n].get("started", ""), held[n].get("at", "")) if n in self.kept_parts else (run.started_at,
                                                                                                   run.ended_at)
        held[n] = {"text": text, "digest": self.stamps[n] if run.status != "budget spent" else "", "run": run.id,
                   "started": written[0], "at": written[1], "landed": False,
                   **({"kept": True} if n in self.kept_parts else {"scores": editor.kept(text)} if text else {})}

    def run_own(self) -> Compiled | None:
        """Run the agent itself on what its subagents returned, when it has instructions of its own and what it reads
        moved: the article's run, ended, when that run waits or fails."""
        c, store, article, subs, composer = self.c, self.c.store, self.article, self.subs, self.composer
        compiled = self.compiled
        self.citable = [_citable(part, by, _articles_read(self.sub_seen.get(n, [])))
                        for n, (part, by) in enumerate(zip(self.parts, self.before))]
        self.reads = [part if n in self.ran and n not in self.holds else part + by
                      for n, (part, by) in enumerate(zip(self.parts, self.before))]
        subs_read = _articles_read([a for seen in self.sub_seen.values() for a in seen])
        self.everything = _citable(self.lead, *self.reads, subs_read)  # one that did not run read by its tools before
        own_now = _stamp_of(self.own_read, *_returned(subs, self.held))
        own_due = self.instructed and (c.scheduled or store.state(_read(composer, article)) != own_now)
        own_wait = ""
        if own_due and not c.scheduled and not c.by_hand:
            own_wait = _backs_off(store, composer, article, own_now, self.now)
        if own_wait:  # its own run failed on what the subagents returned again: the article's run waits as a whole
            return Compiled(None, self.current, skipped=True, waiting=own_wait, runs=compiled.runs)
        for sub, held in zip(subs, self.held):  # a part handed in as a subagent returned it keeps its scores
            self.editor.seed(held.get("text", ""), held.get("scores") or [], sub.output[0])
        if own_due:
            try:
                own = c.run(replace(composer, target=article, prompt=prompts.fill("composer", c.root, drafts=DRAFTS)
                                    + "\n\n" + composer.prompt),
                            [_drafts(subs, self.held), *self.lead], self.own_tools + self.writes.tools(), self.editor)
            except BudgetSpent as error:  # what the subagents returned was paid for: it stands
                own = _not_run(composer, article, error, c.clock)
            self.own = own
            compiled.runs.append(own)
            self.lead = self.lead + _by_tool(self.own_seen)
            self.everything = _citable(self.lead, *self.reads, subs_read, _articles_read(self.own_seen))
            store.set_state(_tool_reads(composer, article), sorted({f.id for f in self.own_seen
                                                                  if isinstance(f, Fact)}))
            _tried(store, composer, article, _stamp_of(digest(composer, self.lead), *_returned(subs, self.held)),
                   c.clock(), own.status not in ("done", WROTE_NOTHING, "budget spent", "held"))
            if own.status == "failed":  # nothing of the article's run lands, as for a subagent's failure: what the
                compiled.run = own  # subagents returned is kept, not landed, for the run that runs again
                return compiled
        compiled.run = self.own or next(iter(compiled.runs), None)
        return None

    def plan_own(self) -> None:
        """What lands of the agent's own run: what stands of what it handed in, or what it composed last."""
        own, composer, current = self.own, self.composer, self.current
        whole = next((s for s in current.compilations if s.by == composer.id and s.whole), None) if current else None
        if own is not None and own.result is not None and own.status in ("done", "budget spent"):
            text, dropped = _landing(self.editor, composer, own.result.text)
            self.compiled.dropped += dropped
            if text:
                _scored(self.c.store, own, self.editor)
                self.plan = [_Part(composer.id, composer.id, text, self.everything, own.started_at, own.ended_at,
                                   whole=True)]
                self.new = True
                self.stood.add(composer.id)
            else:
                if own.status == "done":  # it handed in only what the wiki writes itself: it wrote nothing
                    _wrote_nothing(self.c.store, own, dropped)
                self.composed, self.unstood = whole is not None, whole is None
        elif own is not None and own.status == WROTE_NOTHING and whole is not None:
            # what it composed last gives way to a draft returned since that never landed
            self.drafts = {n for n in range(len(self.subs)) if self.held[n].get("landed") is False
                           and self.held[n].get("text") and n not in self.failed and n not in self.kept_parts
                           and not self.held[n].get("kept")}
            self.drafted, self.composed = bool(self.drafts), not self.drafts
        elif own is not None and own.status not in ("done", WROTE_NOTHING):
            self.compiled.notes.append(f"{composer.id} did not run to its end, since its run {own.status}"
                                       + (f" ({own.note})" if own.note else ""))
            self.leave = any(s.by == composer.id for s in (current.compilations if current is not None else []))
        elif self.instructed and own is None:  # nothing it reads moved: its article stands
            self.leave = any(s.by == composer.id for s in (current.compilations if current is not None else []))

    def join(self) -> None:
        """The subagents' parts, in order, each as it returned it, or as it stands when it did not run, or kept as it
        wrote it last."""
        c, held, rows, plan, ran, failed = self.c, self.held, self.rows, self.plan, self.ran, self.failed
        for n, sub in enumerate(self.subs):
            heading, text, row = sub.output[0], held[n].get("text", ""), rows.get(sub.id)
            stays = None if row is None else _Part(row.heading, sub.id, row.text, {x.id: x for x in row.citations},
                                                   row.started, row.at, digest=row.digest)
            if n in self.kept_parts:  # not written again: what it wrote last
                if text and stays is not None:
                    plan.append(replace(stays, text=text, citable=self.citable[n], digest=held[n].get("digest", "")))
                elif text:  # no part of its own on the article, as under a whole body that gives way: it lands
                    plan.append(_Part(heading, sub.id, text, self.citable[n], held[n].get("started", ""),
                                      held[n].get("at", ""), digest=held[n].get("digest", "")))
                self.new = self.new or stays is None and bool(text) or stays is not None and text != row.text
                continue
            if stays is not None and not text and (n not in ran or n in failed):  # it did not run: its part stays
                plan.append(stays)
                continue
            if not text:
                continue
            if row is not None and row.heading in (heading, _heading(c.store, heading)) and \
                    row.digest == held[n].get("digest") and n not in ran:
                plan.append(stays)
                continue
            plan.append(_Part(heading, sub.id, text, self.citable[n], held[n].get("started", ""),
                              held[n].get("at", ""), digest=held[n].get("digest", "")))
            self.new, self.redrafted = True, self.redrafted or self.drafted and n in self.drafts
            self.stood.add(sub.id)

    def settle(self) -> None:
        """What the plan says of what the agent wrote, once the parts are planned."""
        if self.drafted and not self.redrafted:  # no draft returned since lands: what it composed stands
            self.plan, self.composed, self.new = [], True, False
        if self.composed or self.leave or any(p.whole for p in self.plan):  # a kept part not on the article: the
            for n, at in self.kept_from.items():  # composer's text stands
                if self.kept_parts[n] and not any(p.by == self.subs[n].id for p in self.plan):
                    self.compiled.notes[at] = self.compiled.notes[at].replace(
                        KEPT_LAST, f"so {self.composer.id}'s text stands")
        if self.unstood and self.new:
            self.compiled.notes.append(f"nothing {self.composer.id} handed in stood, so its subagents' content stands")

    def read(self) -> None:
        """Keep the digest the agent's finished run read, so the same does not run it again."""
        store, composer, article = self.c.store, self.composer, self.article
        if not self.instructed and store.state(_read(composer, article)) is not None:
            store.set_state(_read(composer, article), None)  # joined now: instructions put back run again
        if self.instructed and self.own is not None and self.own.status in ("done", WROTE_NOTHING):
            store.set_state(_read(composer, article), _stamp_of(self.own_read, *_returned(self.subs, self.held)))

    def land_sets(self) -> None:
        """Land the fields each run set that lands as `_lands` says, each agent taking in as read what its own writes
        alone moved in what it reads. Another agent's write stays news to it."""
        c, article, own = self.c, self.article, self.own
        for n, run in self.ran.items():
            if _lands(run, self.subs[n].id in self.stood) and (read := _land_own(
                    c, article, self.writers, self.subs[n], self.reads[n], self.sub_writes[n], run.ended_at)) \
                    and self.held[n].get("digest") == self.stamps[n]:  # one not done reading runs again
                self.held[n]["digest"] = _stamp_of(read, self.composer.context)
        if own is not None and _lands(own, self.composer.id in self.stood):
            self.own_read = _land_own(c, article, self.writers, self.composer, self.lead, self.writes,
                                      own.ended_at) or digest(self.composer, self.lead)

    def unlanded(self) -> Compiled:
        """Nothing of the run lands: the article stays as it was, less the lines whose facts were withdrawn, and
        what each run that ran to its end set lands, whatever its text."""
        c, store, article, compiled, own = self.c, self.c.store, self.article, self.compiled, self.own
        self.land_sets()
        self.read()
        _keep_material(store, article, self.subs, self.held, landed=True)
        gone = _withdrawn(c.grounding, store, article, self.writers)
        compiled.dropped += gone
        now_ = store.article(article)
        if now_ is not None and any(s.by in self.writers for s in now_.compilations):
            compiled.notes.append(f"nothing {self.composer.id} handed in stood, and {KEPT}" if self.unstood else KEPT)
        elif self.unstood:
            compiled.notes.append(f"nothing {self.composer.id} handed in stood, and {NONE_STAND}")
        elif self.composed:
            compiled.notes.append(NONE_LEFT)
        elif own is not None and own.status == WROTE_NOTHING and own.note.startswith(STANDS) and not self.composed:
            own.note = f"it added nothing, and {NONE_STAND}{own.note[len(STANDS):]}"
            _wrote_nothing(store, own, [])
        else:
            compiled.notes.append(NONE_STAND)
        if gone or self.writes.sets or any(w.sets for w in self.sub_writes.values()):
            _copy(store, article, store.article(article), c.root, c.grounding)
        return compiled

    def land(self) -> Compiled:
        """Land the plan in one write, with the material and what each run set, or keep the article as it was."""
        c, store, article, compiled, own, plan = self.c, self.c.store, self.article, self.compiled, self.own, self.plan
        if compiled.runs and not self.new and not self.leave:
            return self.unlanded()
        current = self.current
        mine = [s for s in (current.compilations if current is not None else []) if s.by in (*self.writers, *self.orphans)]
        if not plan and not mine or self.leave:
            self.land_sets()
            if not self.leave or own is None:
                _keep_material(store, article, self.subs, self.held, landed=True)
            self.read()
            if self.leave and not compiled.runs:  # no run, and nothing it reads moved: the article stands, unwritten
                return Compiled(None, current, skipped=True)
            if self.leave:  # no text of this run lands: what stood, less each line whose every cited fact was withdrawn
                compiled.dropped += _withdrawn(c.grounding, store, article, self.writers)
            return compiled if not self.leave else _finish(store, article, c.root, compiled, c.grounding)
        if fixed(self.composer) and plan:
            birth(store, self.composer, article, min((p.started for p in plan if p.started), default=stamp(c.clock())))
        for n, editor in self.editors.items():  # what each subagent's text that lands was scored, on its run's row
            if self.subs[n].id in self.stood:
                _scored(store, self.ran[n], editor)

        def after() -> None:
            self.land_sets()
            _keep_material(store, article, self.subs, self.held, landed=True)
            self.read()
            if not self.instructed:  # no model ran, so this row is what status shows of it
                _no_model_row(store, self.composer, article, "joined", stamp(c.clock()),
                              f"joined {len(plan)} {'part' if len(plan) == 1 else 'parts'}, with no model call and "
                              "no cost")
            for n, run in self.ran.items():
                if n not in self.failed:
                    _keep_left(store, self.subs[n], article, self.parts[n], run.started_at)
            if own is not None and own.status in ("done", WROTE_NOTHING):
                _keep_left(store, self.composer, article, self.lead, own.started_at)

        try:
            cleaned, noted = _land(store, article, plan, "", [*self.writers, *self.orphans], after)
            compiled.cleaned += cleaned
            compiled.notes += noted
        except _LeftAlone as left:
            return replace(compiled, article=store.article(article), skipped=True, waiting=str(left), left_alone=str(left))
        if stood := [p.by for p in plan if p.by not in self.stood]:  # a part its writer left as it was, not text new
            compiled.dropped += _withdrawn(c.grounding, store, article, stood)  # to this run, which the editor scored
        return _finish(store, article, c.root, compiled, c.grounding)


ESTIMATE_MARGIN = 2.0  # a run is held at this many times what the agent's last run spent
# A first run is held at its opening read this many times, as a run sends it again on each turn, and a reply of
# FIRST_REPLY tokens.
FIRST_TURNS = 4
FIRST_REPLY = 4_000


def _estimate(store: Store, agent: Agent, evidence: list[Evidence], model: ModelSpec | None) -> float | None:
    """What a run of `agent` is held at against the caps: `ESTIMATE_MARGIN` times what its last run spent, or for an
    agent with no run that spent anything yet, its opening read `FIRST_TURNS` times and a reply, priced on `model`.
    None, its whole token budget, when neither can be priced."""
    last = next((row.data["run"] for row in reversed(store.rows("agent-runs", {"agent": agent.id}))
                 if row.data.get("run")), "")
    spent = store.total(LOG, "usd", {"run": last}) if last else 0.0
    if spent > 0:
        return spent * ESTIMATE_MARGIN
    if model is None or price_of(model.provider, model.model) is None:
        return None
    opening = tokens(agent.prompt) + sum(tokens(entry.heading) + sum(tokens(line) for line in entry.lines)
                                         for entry in evidence)
    return cost(model.provider, model.model, TokenCount(opening * FIRST_TURNS, FIRST_REPLY))


def _cut(text: str, scrub: Callable[[str], str] | None, most: int) -> str:
    """`text` masked by `scrub`, then cut to `most` characters, never inside a mask: cut first, a secret across the
    cut would no longer match its mask."""
    text = scrub(text) if scrub else text
    if len(text) <= most:
        return text
    for mask in (MARK, MASKED):
        at = text.rfind(mask, 0, most + len(mask) - 1)
        if at >= 0 and at + len(mask) > most:
            most = at
    return text[:most]


def run_agent(agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], *, agent_run: AgentRun,
              store: Store, ledger: Ledger | None = None, model: ModelSpec | None = None,
              clock: Clock = system_clock, review: Callable[[str], str] | None = None, run_id: str = "") -> Run:
    """The part every agent's run shares, a writer's, a subagent's, a composer's or a curator's: reserve what the run
    may spend under the compilation cap, as `_estimate` gives it, run the agent with its tools on its evidence, its
    first hand-in shown to `review`, under `run_id` when its editor minted one, and in every outcome, a run that fails
    or raises included, record what it spent in the ledger and a row in the `agent-runs` log, then let the reservation
    go. Raises `BudgetSpent` before the run when the cap is reached, or runs under way already hold what is left of
    it, as `HeldForRoom`."""
    if stopping(store):
        raise Stopped(f"serve is stopping, so {agent.id} does not run: it runs on a later pass")
    ticket = ledger.reserve("compilation", clock(), model, agent.budget.tokens_per_run,
                            usd=_estimate(store, agent, evidence, model)) if ledger is not None else None
    run: Run | None = None
    interrupted = False  # an interrupt goes on up as itself, not as a stop of serve
    try:
        try:
            reviewing, minted = REVIEW.set(review), RUN_ID.set(run_id)
            try:
                with under(store):
                    run = agent_run.run(agent, evidence, tools, agent.budget)
            finally:
                REVIEW.reset(reviewing)
                RUN_ID.reset(minted)
        except BaseException as stopped:  # an interrupt: the turns it made before it count
            run, interrupted = getattr(stopped, "partial_run", None), True
            raise
        if said := [note for entry in evidence for note in entry.notes if note.startswith(SUMMARY_NOTE)]:
            run.note = "; ".join([run.note, *said] if run.note else said)
        return run
    finally:
        spent = run.usage if run else Usage()
        if ledger is not None and model is not None:
            ledger.record("compilation", clock(), model, spent, run.id if run else "")
        if ledger is not None and ticket is not None:
            ledger.release(ticket)  # settled: what it spent is in the ledger now
        store.append("agent-runs", {
            "run": run.id if run else "", "agent": agent.id, "target": agent.target,
            "status": run.status if run else "failed", "provider": model.provider if model else "",
            "model": model.model if model else spent.model, "requests": spent.requests,
            "tool_calls": spent.tool_calls, "input_tokens": spent.input_tokens, "output_tokens": spent.output_tokens,
            "ended_at": run.ended_at if run else stamp(clock()),
            **({"note": _kept_note(store, run.note)} if run and run.note else {}),
            **({"refused": len(run.refused), "refused_tools": run.refused} if run and run.refused else {})})
        if run is not None and run.status == STOPPED and not interrupted:
            raise Stopped(f"serve stopped {agent.id}'s run before it ended: it runs again on a later pass")
