"""The app object, `Cerebro`: one workspace, opened from its folder, and all a command, the scheduler, an app or a
test calls on it. There is no global active app: whatever needs the workspace is handed this object.

Opening a workspace reads its settings and secrets store, builds its sources' connectors, loads its apps and agents,
and opens its store, refusing in one line what does not fit. Its methods return data, and the CLI prints. They fall in
groups: masking, the pass and agent runs, models and their keys, building connectors, the six operations, and what
the workspace holds. Two rules hold in all of them:

- One clock, `clock`, is the time every piece reads.
- Every value the secrets store holds is masked in all that reaches a model, the store, a log or a caller. A connector
  and the decision model come wrapped (`Masked`, `MaskedDecider`), and a result is masked on its way out. No connector
  reads a key by itself: each is handed a login that reads the store when it is called.

    workspace = Cerebro(Path("notes-wiki"), connectors=[...], extraction=..., trusted=True)
    result = workspace.run_once()
    workspace.read(Caller("you", "person"), ["reports/weekly"])
"""
from __future__ import annotations

import functools
import html
import os
import re
import sys
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Self
from urllib.parse import unquote, urlsplit

import httpx

from . import prompts, runner
from .agent import Agent, load_agents, refuse_unknown_kinds, refuse_unknown_tools
from .apps import App, Exposed, bind, check_apps, collect
from .article import pending
from .budget import Held, Ledger, Metered
from .caller import Caller
from .clock import Clock, system_clock
from .compile import Compiled, _workspace, compile_article
from .connector import Ask, Connector, Listing, Offer, Ours, Record, RecordNotFound, masking, one_line
from .connectors import CONNECTORS
from .connectors.mcp_oauth import LiveToken, is_login
from .connectors.mcp_source import McpSource
from .connectors.mcp_source_client import Credentials, McpClient, reach
from .curate import (
    Curated,
    cited,
    decided,
    for_a_person,
    may_answer,
    nothing_to_weigh,
    run_curator,
    unbuilt,
    working_set,
    workspace_weights,
)
from .errors import CerebroError
from .evidence import BUILTIN, STDERR_BYTES, Finished, Launch, start
from .facts import STATE as FACTS_STATE
from .facts import Extraction, FactoidExtraction, Revisions, kind_notices, revisions_kept
from .gateway import NO_KEY, Gateway, read_providers
from .ground import TALLY
from .lookup import (
    DecideResult,
    Decision,
    FindResult,
    OperationError,
    QueryResult,
    ReadResult,
    RetractResult,
    StoreLookup,
    TellResult,
)
from .models import PROVIDERS, Decider, HttpDecider, ModelSpec, Question, YesNo, read_model
from .models import Decision as Decided
from .private import SIDE_FILES, tighten
from .project import Project, WorkspaceError, agent_notices, fiscal_now, load, min_grounding, read_fiscal, read_ours
from .records import SLUG_MOST, TOLD, Proposal, Review, proposals, review, told_waiting
from .rendered import descriptions
from .reply import outside
from .rerank import RERANK_CALL_SECONDS, DecisionReranker, read_rerank
from .run import (
    WROTE_NOTHING,
    AgentRun,
    Entry,
    PydanticRewriter,
    PydanticRun,
    RewriteFailed,
    Rewriter,
    Run,
    masked,
    masked_error,
)
from .runner import PassResult, Progress, run_pass
from .search import Index
from .secrets import (
    _HIDDEN,
    MARK,
    MIN_SCRUB_LENGTH,
    STORE_ENV,
    EveryValue,
    FileSecrets,
    Mask,
    SecretNotFound,
    Secrets,
    SecretsError,
    login_getter,
    login_name,
    model_key,
    open_secrets,
    sign_in_name,
)
from .source import SourceError, SourceFile, Sources, load_sources, read_source
from .store import DEFAULT_PATH, SQLiteIndex, SQLiteStore, Store, open_store, scrubbed
from .tokens import Tokens
from .workspace import Workspace

__all__ = ["KINDS", "Cerebro", "Masked", "MissingModelKey", "PassResult", "TokenIsLogin"]

KINDS = tuple(CONNECTORS)
EXTRACTION_INPUT_BYTES = 48_000  # the most one question to the extraction model holds, its prompt included


class MissingModelKey(SecretsError):
    """An agent would run on a model whose key neither the secrets store nor the environment holds."""


class TokenIsLogin(SecretsError):
    """A secret a source would send as its token holds a whole browser sign-in record instead."""


# -- what reaches a model, the store or a caller is masked -------------------------------------------------------------
class MaskedDecider(Decider):
    """The decision model as every caller of it gets it: each string of what it is sent, the state and each
    question's words, masked first of every value the workspace's secrets store holds, by the mask `masks` gives. So
    grounding's judge, the keep judge, the near-match judge, the tell sort, the eval judge and the re-rank all send
    masked text, however each builds its request."""

    def __init__(self, inner: Decider, masks: Callable[[], Callable[[str], str]]):
        self.inner, self.masks = inner, masks

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decided:
        mask = self.masks()
        return self.inner.decide(masked(state, mask), {name: masked(q, mask) for name, q in questions.items()})

    def bounded(self, seconds: float) -> Decider:
        """The same, its model asking once with the time bound `HttpDecider.bounded` sets, when it has one."""
        inner = self.inner.bounded(seconds) if isinstance(self.inner, HttpDecider) else self.inner
        return MaskedDecider(inner, self.masks)


_FRESH = object()  # no listing under way: the next page asked for starts one


class Masked(Connector):
    """A connector whose records come with every value the workspace's secrets store holds masked in their bodies and
    fields, so a token pasted into an issue never reaches a model or the store. The connector is handed the same mask
    for the length of each call, so it masks what it cuts before it cuts it. A curator's question goes through the
    same mask: each row's keys and fields, each notice, and a refusal's line. An id is never masked, since a masked
    id no longer names its record: a record or a row whose id holds a stored value is left out whole, and a notice
    names it by what a person finds it by at the source, masked: its title, its link, else its time. A listing that
    leaves one out says so in `again`, so the runner lists it again from where that listing began. Anything else it
    is asked for is the connector's own. The mask is built once for each call, from the store as it stands."""

    def __init__(self, inner: Connector, masks: Callable[[], Callable[[str], str]],
                 ready: Callable[[], None] | None = None):
        self.inner, self.name, self._masks = inner, inner.name, masks
        self.lists_nothing, self.IDENTITY_KEYS = inner.lists_nothing, inner.IDENTITY_KEYS
        self.speaker_pattern = inner.speaker_pattern
        self._next: object = _FRESH  # the cursor the next page of this listing is asked from
        self._named: dict[str, int] = {}  # how many records left out in this listing were named each way
        self._ready = ready  # raises while the connector cannot tell whose a record is

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    @staticmethod
    def _record(record: Record, mask: Callable[[str], str]) -> Record:
        """`record` with every part of it but its id masked, the names of its keys and fields too: its link, time,
        revision, keys, links, fields and body. A part that holds no stored value is left as it was."""
        record.revision, record.at, record.url = masked(record.revision, mask), masked(record.at, mask), \
            masked(record.url, mask)
        keys: dict[str, list[str]] = {}
        for name, values in record.keys.items():
            name, values = masked(name, mask), masked(values, mask)
            if name in keys:  # two names that differ only in a stored value keep the values of both
                keys[name] = [*keys[name], *(value for value in values if value not in keys[name])]
            else:
                keys[name] = values
        record.keys, record.links = keys, masked(record.links, mask)
        record.fields = masked(record.fields, mask)
        if record.body is not None:
            record.body = masked(record.body, mask)
        return record

    @staticmethod
    def _called(record: Record, mask: Callable[[str], str]) -> str:
        """What a person finds a record by at its source, masked, never its id: its title and its link, else its
        time. A link is read with its escapes undone, so a value it holds escaped is masked too."""
        title = next((" ".join(value.split()) for key in ("title", "subject", "name")
                      if isinstance(value := record.fields.get(key), str) and value.strip()), "")
        title = f'"{one_line(mask(title), 80)}"' if title else ""
        link = one_line(mask(unquote(record.url)), 160) if record.url else ""
        at = f"of {one_line(mask(record.at), 40)}" if record.at else ""
        if title:
            return f"{title} at {link}" if link else f"{title} {at}".strip()
        return f"at {link}" if link else at or "with no title, link or time"

    def _kept(self, records: list[Record], mask: Callable[[str], str], noun: str,
              named: dict[str, int]) -> tuple[list[Record], list[str]]:
        """The records whose id holds no stored value, masked, and a line for each one left out, which names it as
        `_called` does, never by its id, and counts one named as another was, so no two lines are the same."""
        kept, said = [], []
        for record in records:
            if mask(str(record.id)) != str(record.id):
                called = self._called(record, mask)
                named[called] = named.get(called, 0) + 1
                more = f" (number {named[called]} of that name)" if named[called] > 1 else ""
                said.append(f"left out the {noun} {called}{more}: its id holds a value the secrets store keeps, so "
                            "nothing of it is kept or sent: change the id at the source, or the stored value")
            else:
                kept.append(self._record(record, mask))
        return kept, said

    def list(self, since: str | None = None) -> Listing:
        if self._ready is not None:
            self._ready()
        mask = self._masks()
        with masking(mask):
            listing = self.inner.list(since)
        if since != self._next:
            self._named = {}
        listing.records, said = self._kept(listing.records, mask, "record", self._named)
        listing.notices, listing.again = [*listing.notices, *said], bool(said)
        self._next = listing.cursor if listing.more else _FRESH
        return listing

    def get(self, id: str) -> Record:
        if self._ready is not None:
            self._ready()
        mask = self._masks()
        with masking(mask):
            found = self.inner.get(id)
        if mask(str(found.id)) != str(found.id):
            raise RecordNotFound(f"{self.name}: left out the record asked for {self._called(found, mask)}: its id "
                                 "holds a value the secrets store keeps, so nothing of it is kept or sent")
        return self._record(found, mask)

    @property
    def questions(self) -> Mapping[str, str]:  # type: ignore[override]
        return self.inner.questions

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        mask = self._masks()
        try:
            with masking(mask):
                answer = self.inner.ask(question, params, limit=limit)
        except CerebroError as error:
            error.args = (mask(str(error)),)
            raise
        answer.records, said = self._kept(answer.records, mask, "row", {})
        for row in answer.records:
            row.keys = scrubbed(row.keys, mask)
        answer.notices = [mask(line) for line in [*answer.notices, *(f"{self.name}: {line}" for line in said)]]
        return answer

    def offer(self, ask: Ask | None = None) -> Offer | None:
        """What there is to choose for the source's file, each row's value and words and every note masked. What the
        file lists now is the person's own text, and comes as it is."""
        mask = self._masks()
        with masking(mask):
            offered = self.inner.offer(ask)
        if offered is None:
            return None
        return replace(offered, choices=tuple(replace(c, value=mask(c.value), label=mask(c.label),
                                                      detail=mask(c.detail), group=mask(c.group))
                                              for c in offered.choices),
                       note=tuple(mask(line) for line in offered.note))

    def written(self, text: str, offer: Offer, values: list[str]) -> str:
        return self.inner.written(text, offer, values)


# -- masking what a script printed past its limit ----------------------------------------------------------------------
_SKIPPED = re.compile(rf"[\s>]|{_HIDDEN.pattern}")  # what a mask lets stand between a value's characters
_STOPPED_INSIDE = re.compile(r"(?:&#?\w*|\\)\Z")  # an entity or a markdown escape a cut stopped inside
_ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")


def _bare(text: str) -> str:
    """`text` as the characters of a value it may spell: entities and markdown escapes read, and without white space,
    wraps' quote marks or hidden characters, in lower case."""
    return _SKIPPED.sub("", _ESCAPED.sub(r"\1", html.unescape(_STOPPED_INSIDE.sub("", text))).lower())


def _without_head(text: str, forms: Iterable[str], room: int, mask: Callable[[str], str]) -> str:
    """`text`, which a cut ended, masked by `mask`, without a tail in its last `room` bytes that may begin a form of a
    stored value, as written, or spelled and wrapped as `Scrubber` finds it. The tail is found before the mask, which
    would hide a shorter value a form starts with and leave the rest of its head. What is left before a tail is read
    again, since a value may end where the tail begins. A tail of six characters or more ends in `[secret]` instead. A
    tail that begins no form stays."""
    lowered = {f.lower() for f in forms if f}
    bare = {b for f in lowered for b in (_bare(f), _bare(html.unescape(f))) if b}
    firsts = {f[0] for f in lowered} | {b[0] for b in bare} | {"&", "\\"}
    data = text.encode()
    floor, end = len(data[:max(len(data) - room, 0)].decode("utf-8", "ignore")), len(text)
    for _ in range(len(lowered)):
        if (at := _head_at(text[:end], floor, lowered, bare, firsts)) == end:
            break
        end = at
    return mask(text[:end]) + (MARK if len(_bare(text[end:])) >= MIN_SCRUB_LENGTH else "")


def _head_at(text: str, floor: int, lowered: set[str], bare: set[str], firsts: set[str]) -> int:
    """Where the longest tail of `text` from `floor` on that may begin a form starts, or the length of `text`."""
    for at in range(floor, len(text)):
        if text[at].lower() not in firsts:
            continue
        near, seen = text[at:at + 64].lower(), _bare(text[at:at + 64])
        if not any(f.startswith(near) for f in lowered) and seen and not any(b.startswith(seen) for b in bare):
            continue
        tail = text[at:]
        if any(f.startswith(tail.lower()) for f in lowered) or any(b.startswith(_bare(tail)) for b in bare):
            return at
    return len(text)


# -- refusals while a workspace opens ----------------------------------------------------------------------------------
def _reserved(name: str) -> str:
    """Why a source may not take `name`, or "": Cerebro keeps what people tell it as records of the source `tell`,
    and its own state keys hold a colon."""
    if name == TOLD:
        return "the source Cerebro keeps what people tell it under"
    if ":" in name or name.startswith("#"):
        return "not a plain name: it holds a colon or starts with #, as Cerebro's own state keys do"
    return ""


def _refuse_duplicates(agents: list[Agent]) -> None:
    """Refuse two agents with one id, or two that write one article or curate one collection."""
    seen: dict[tuple[str, str], Agent] = {}
    for agent in agents:
        keys = [("id", agent.id)]
        if agent.role in ("writer", "article", "composer"):
            keys.append(("writes", agent.target))
        elif agent.role == "curator":
            keys.append(("curates", agent.target))
        for key in keys:
            if key in seen:
                raise WorkspaceError(f"{seen[key].path} and {agent.path} both {'have the id' if key[0] == 'id' else key[0]}"
                                 f" {key[1]}: rename or remove one of them")
            seen[key] = agent


# -- what a connector is built with ------------------------------------------------------------------------------------
@functools.cache
def _tls() -> Any:
    """The one TLS context every workspace in this process sends with, read from the system's or the environment's
    certificates once: loading them takes longer than most requests."""
    return httpx.create_ssl_context()


# -- the arguments an outside agent sends an operation -----------------------------------------------------------------
def _takes(name: str, args: Mapping[str, Any], *keys: str, need: tuple[str, ...] = ()) -> None:
    """Refuse the arguments `args` sent to the operation `name` unless they hold `keys` alone, each of `need` among
    them."""
    unknown = sorted(set(args) - set(keys))
    if unknown or any(n not in args for n in need):
        needs = f", and needs {', '.join(need)}" if need else ""
        nots = f", not {', '.join(unknown)}" if unknown else ""
        raise OperationError(f"{name} takes {', '.join(keys)}{needs}{nots}: send those keys alone")


def _decisions(name: str, entries: Any) -> list[str | Decision] | None:
    """An accept or reject list as an outside agent sends it to `decide`: proposal ids, or `{id, reason}`."""
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise OperationError(f"{name}: accept and reject are lists of proposal ids, or of {{id, reason}}")
    return [e if isinstance(e, str) else Decision(str(e.get("id", "")), str(e.get("reason", "")))
            for e in entries if isinstance(e, (str, dict))]


class Cerebro:
    """One workspace, loaded from its folder. It loads only when `trusted` is True or its owner trusts it, as `trust`
    says: an untrusted folder is refused in one line, before anything of it is read or run."""

    def __init__(self, root: Path | None = None, *, store: Store | None = None, index: Index | None = None,
                 agent_run: AgentRun | None = None, extraction: Extraction | None = None,
                 connectors: Iterable[Connector] = (), apps: Iterable[App] = (), clock: Clock | None = None,
                 secrets: Secrets | None = None, http: httpx.Client | None = None, decider: Decider | None = None,
                 trusted: bool | None = None):
        project = load(root, trusted=trusted)
        self.clock = clock or system_clock
        self.root = project.root
        self.settings = project.settings
        self.models = project.models
        self.zone = project.zone
        self.index = index
        self.gateway = Gateway(read_providers(self.settings), self.clock, project.zone, secret=self._header_secret)
        self._own_extraction = extraction is None
        self.extraction = extraction or FactoidExtraction(self.root, self.models["extraction"],
                                                          key=self._compile_key, kinds=project.kinds,
                                                          gateway=self.gateway, mask=lambda text: self.mask_every()(text),
                                                          speakers=lambda source: getattr(
                                                              self.connectors.get(source), "speaker_pattern", ""))
        own_id = self.settings.get("id")
        self.secrets = secrets if secrets is not None or not own_id else open_secrets(str(own_id), root=self.root)
        self.mask = Mask()
        self.every = EveryValue(self.secrets) if self.secrets is not None else None  # this workspace's alone
        try:
            self.ours: Ours = read_ours(self.root)
        except WorkspaceError as error:  # reads go on: `status`, a pass and a compile name the fault
            self.ours, self._unread = Ours((), ()), str(error)
        else:
            self._unread = ""
        self._http, self._own_http = http, False
        self.sf_run: Callable[..., Any] | None = None  # how a command-line login runs, so a test can stand in
        self.script_start: Callable[[Launch], Finished] = start  # how a script line's script runs, likewise
        self.imap_connect: Callable[[str], Any] | None = None  # how a mail login opens its connection, likewise
        self.mcp_open: Callable[..., McpClient] = McpClient  # how an MCP session opens, from a `Reach`, likewise
        self._mcp: dict[str, McpClient] = {}
        self._mcp_lock = threading.Lock()
        sources = load_sources(self.root, {kind: functools.partial(CONNECTORS[kind].build, app=self) for kind in KINDS})
        self.connectors = self._masked_connectors(sources, connectors)
        self._own_run = agent_run is None
        self.agent_run = self._agent_run(agent_run)
        self.apps = [*project.apps, *(app for app in apps if not any(app is mine for mine in project.apps))]
        check_apps(self.apps)
        for kind in (kind for app in self.apps for kind in app.kinds if kind in self.connectors):
            raise WorkspaceError(f"an app declares kind {kind}, which is already a source of this workspace: rename one")
        self.refused = list(project.refused) + sources.refused
        self.notices = list(project.notices) + sources.notices + prompts.outdated(self.root)
        self.notices += [f"sources/{name}: {c.lists_nothing}, so a pass does not list it"
                         for name, c in self.connectors.items() if c.lists_nothing]
        if isinstance(self.secrets, FileSecrets) and secrets is None:
            why = ("CEREBRO_SECRETS_STORE=file asks for it" if os.environ.get(STORE_ENV) == "file"
                   else "no keychain is usable here")
            self.notices.append(f"secrets: {why}, so this workspace's keys and logins are kept in plain text in "
                                f"{self.secrets.where()}, which only your user can read")
        found = decider if decider is not None else self._decider()
        self.decider = MaskedDecider(found, self.mask_every) if found is not None else None
        self.agents, self.exposed, refused, self.notices = self._agents_and_functions(project, self.notices)
        self.refused += refused
        self._slugged: dict[Callable[[str], str], tuple[Any, Any, list[str]]] = {}  # by slug: the forms, slugged
        self.store = self._open_store(store)
        if self.extraction is not None:  # earlier saves of a record stay readable, in the store
            self.extraction = Revisions(self.extraction, self.store, keep=revisions_kept(self.settings),
                                        clock=self.clock)
        self.ledger = Ledger(self.store, project.caps, self.gateway.check)
        self.asker: Rewriter | None = None  # what `ask_model` asks, the extraction model when None
        kinds = [kind for app in self.apps for kind in app.kinds]
        if self.index is None:  # the search index in the store's own file, its tables made here and not in a read
            self.index = SQLiteIndex(self.store if isinstance(self.store, SQLiteStore) else None)
        self.lookup = self._lookup(kinds)
        self.handle = Workspace(self.lookup, self.store, kinds, self.clock, mask=self.mask_every)

    def _masked_connectors(self, sources: Sources, handed: Iterable[Connector]) -> dict[str, Connector]:
        """Each connector built from `sources/` and each handed in, by name, in `Masked`. A name Cerebro keeps for
        its own is refused: in one line for a connector handed in, as a refused file for one built."""
        for connector in handed:
            if why := _reserved(connector.name):
                raise WorkspaceError(f"a connector is named {connector.name}, {why}: name it another way")
        for built in [c for c in sources.connectors if _reserved(c.name)]:
            sources.connectors.remove(built)
            sources.refused.append(f"sources/{built.name}.yaml: the name {built.name} is {_reserved(built.name)}: "
                                   "rename the file")
        return {c.name: Masked(c, self.mask_every, self._knows_ours) for c in [*sources.connectors, *handed]}

    def _agent_run(self, handed: AgentRun | None) -> AgentRun:
        """What agents run on: the run handed in, as a test's is, else the compilation model of settings."""
        if handed is None:
            return PydanticRun.from_spec(self.models["compilation"], clock=self.clock, zone=self.zone, root=self.root,
                                         key=self._compile_key, masks=self.mask_every, gateway=self.gateway)
        if isinstance(handed, PydanticRun) and handed.masks is None:
            handed.masks = self.mask_every
        return handed

    def _agents_and_functions(self, project: Project,
                              notices: list[str]) -> tuple[list[Agent], Exposed, list[str], list[str]]:
        """The workspace's agents and each app's, the functions the workspace and its apps expose, a line for each
        agent file refused, and `notices` with what loading adds, less those of a refused agent. Two agents with one
        id or one target, or an evidence function named for a built-in kind, are refused in one line; an agent whose
        evidence reads a kind nothing gives, or that lists a tool nothing runs in its file, is refused alone."""
        agents, functions, refused = list(project.agents), list(project.extensions), []
        notices = list(notices)
        for app in self.apps:
            functions += app.functions
            if app.agents is not None:
                loaded, not_loaded = load_agents(Path(app.agents))
                agents += loaded
                refused += not_loaded
                notices += agent_notices(loaded)
        _refuse_duplicates(agents)
        notices += unbuilt(agents)
        exposed = collect(functions)
        for name in exposed.evidence:
            if name in BUILTIN:
                raise WorkspaceError(f"evidence function {name} shadows the built-in kind {name}: rename it")
        kinds = {**dict.fromkeys(BUILTIN), **exposed.evidence}
        agents, unknown, notices = refuse_unknown_kinds(agents, kinds, notices)
        agents, untooled, notices = refuse_unknown_tools(agents, exposed.agent_tools, notices)
        return agents, exposed, refused + unknown + untooled, notices

    def _lookup(self, kinds: list[str]) -> StoreLookup:
        """What the six operations read and write through: the store and its index, the facts layer metered against
        its cap, the decision model, and a re-rank of `matching` when settings ask for one."""
        metered = Metered(self.extraction, self.ledger, self.models["extraction"], self.clock) \
            if self.extraction is not None else None
        return StoreLookup(self.store, metered, kinds, self.index, clock=self.clock, zone=self.zone,
                           fiscal=lambda: fiscal_now(self.root), decider=self.decider, ledger=self.ledger,
                           decision_model=self.models["decision"], root=self.root, rerank=self._reranker(metered),
                           agent_text=[kind for app in self.apps for kind in app.text_for_agents],
                           masks=self.mask_every,
                           collections=lambda: {a.target.partition("/")[0] for a in self.agents if a.target})

    def _open_store(self, handed: Store | None) -> Store:
        """The store handed in, else the workspace's own, opened once the private files are the owner's alone. Either
        way it masks what it keeps, as `_store_masks` says."""
        store = handed
        if store is None:
            self.notices += tighten(self._private_paths(), self.root)
            store = open_store(self.root, self.settings)
        store.scrub, store.masks = self.mask, self._store_masks
        store.weights = workspace_weights(self.connectors.values())
        return store

    def _private_paths(self) -> list[Path]:
        """What only the owner may read: the state folder, the mirrored articles, and a SQLite store's file and the
        files beside it wherever settings put it. A folder settings name for the store is left as it is."""
        paths = [self.root / ".cerebro", self.root / "articles"]
        block = self.settings.get("store")
        block = block if isinstance(block, dict) else {}
        path = block.get("path") if block.get("class", "sqlite") == "sqlite" else None
        if isinstance(path, str) and path.strip():
            file = Path(path).expanduser()
            file = file if file.is_absolute() else self.root / file
            paths += [file, *(file.with_name(file.name + suffix) for suffix in SIDE_FILES)]
        return paths

    def close(self) -> None:
        """Close the store, the Factoid app the workspace built, if it built one, every MCP session a source opened, and
        the HTTP client if one was opened here."""
        if self._own_extraction and self.extraction is not None:
            self.extraction.close()  # type: ignore[attr-defined]
        self.store.close()
        with self._mcp_lock:
            sessions, self._mcp = list(self._mcp.values()), {}
        for session in sessions:
            session.close()
        for connector in self.connectors.values():
            if isinstance(connector.inner if isinstance(connector, Masked) else connector, McpSource):
                connector.close()
        if self._http is not None and self._own_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: object, error: BaseException | None, trace: object) -> None:
        """Close, and mask the error that ends the block as `safe_error` masks it, so whoever prints it, such as a
        command's one line, prints no value the secrets store holds."""
        try:
            if isinstance(error, Exception) and (safe := self.safe_error(error)) is not error:
                raise safe from None
        finally:
            self.close()

    # -- masking what leaves the workspace -----------------------------------------------------------------------------
    def mask_every(self) -> Callable[[str], str]:
        """A mask of every value the workspace's secrets store holds, as well as each value read so far: for what a
        script, a query, a caller or a source gives, which may hold a secret nothing here has read yet. The values
        come from `every`, which reads the store again only by its rule, and at the start of each pass and run."""
        if self.every is None:
            return self.mask
        every = self.every()
        return lambda text: self.mask(every(text))

    def refresh_mask(self) -> None:
        """Read the secrets store again before the next masked write, to take a secret another process added. When
        the store cannot be read, this raises `SecretsError` with the line that says why and how to mend it, so no
        pass, compile or curator run starts."""
        if self.every is not None:
            self.every.refresh()
            self.every()

    def _store_masks(self, slug: Callable[[str], str] | None = None) -> Callable[[str], str]:
        """The masks the store is handed: `mask_every`'s, and with `slug`, a mask of an article's id that `slug`
        made from a name. It masks each value the secrets store keeps or the mask read as `slug` makes it, and, in
        a name as long as a slug gives, a head of one of six characters or more at the cut. No other mask finds a
        value in a slug, and an id is never masked, so an admit whose id it changes is refused."""
        if slug is None:
            return self.mask_every()
        held = getattr(getattr(self.mask, "_scrubber", None), "patterns", ())
        every = self.every().patterns if self.every is not None else ()
        was = self._slugged.get(slug)
        if was is None or was[0] is not held or was[1] is not every:
            slugged = {slug(f) for f in (*held, *every) if not (len(f) <= 10 and f.endswith("..."))}  # a YAML cut
            self._slugged[slug] = (held, every, sorted((s for s in slugged if len(s) >= MIN_SCRUB_LENGTH), key=len,
                                                       reverse=True))
        found = self._slugged[slug][2]

        def mask(id: str) -> str:
            out = id
            for s in found:
                out = out.replace(s, MARK)
            if len(id.rpartition("/")[2]) >= SLUG_MOST - 1:  # a name cut to its most, which may lose its last dash
                head = max((n for s in found for n in range(MIN_SCRUB_LENGTH, len(s)) if out.endswith(s[:n])), default=0)
                out = out[:len(out) - head] + MARK if head else out
            return out

        return mask

    def masked(self, value: Any) -> Any:
        """A result with every string in it, in its records and lists too, masked of each value read from the
        secrets store, as each surface shows it."""
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self.masked(item) for item in value]
        if isinstance(value, dict):
            return {key: self.masked(item) for key, item in value.items()}
        if is_dataclass(value) and not isinstance(value, type):
            return replace(value, **{f.name: self.masked(getattr(value, f.name)) for f in fields(value) if f.init})
        return value

    def _mask_run(self, run: Run) -> None:
        """Mask every value the secrets store holds in a run's note and transcript, in place."""
        run.note = self.mask_every()(run.note)
        run.transcript = [Entry(e.role, self.mask_every()(e.text), e.tool,
                                scrubbed(e.args, lambda text: self.mask_every()(text))) for e in run.transcript]

    def safe_error(self, error: BaseException | None = None) -> BaseException:
        """`error`, or the one being handled, with every value the secrets store holds masked in its text and its
        chain, as `run.masked_error` masks it, so a screen or a log may print it, its traceback too. When the store
        cannot be read, the error that says so stands in for it, since nothing may print unmasked."""
        error = error if error is not None else sys.exc_info()[1]
        if error is None:
            return RuntimeError("no error")
        if not str(error) and error.__cause__ is None and error.__context__ is None:
            return error  # nothing to mask, such as a command's exit
        try:
            mask = self.mask_every()
        except SecretsError as failed:
            return failed
        return masked_error(error, mask)

    def start_script(self, launch: Launch) -> Finished:
        """Run a `script` evidence line's script, as `evidence.start` does, and mask in what it printed every value
        the secrets store holds, read or not, so a script that reads a key from a file hands no model its value. A
        test sets `script_start` to stand in for the process."""
        done = self.script_start(launch)
        mask = self.mask_every()
        room = 8 * getattr(self.mask, "longest", 0)  # a value's longest form, spelled out or wrapped
        held = getattr(getattr(self.mask, "_scrubber", None), "patterns", None)
        forms = [*(held or ())]
        if self.every is not None:
            room = max(room, 8 * self.every().longest)
            forms += self.every().patterns

        def shown(data: bytes, cut: bool) -> bytes:
            for trim in range(4 if cut else 1):  # output cut at its limit may end inside a character
                try:
                    text = data[:len(data) - trim].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if cut and held is None and room:  # the forms read so far are unknown: drop all a head may fill
                    text = mask(text)
                    text = text.encode()[:max(len(text.encode()) - room, 0)].decode("utf-8", "ignore")
                elif cut:  # the read stopped at the limit, so its end may hold the head of a value no mask finds
                    text = _without_head(text, forms, room, mask)
                else:
                    text = mask(text)
                return text.encode()
            return data  # not text: gather refuses it, and nothing of it reaches a model

        return replace(done, stdout=shown(done.stdout, done.cut),
                       stderr=shown(done.stderr, len(done.stderr) >= STDERR_BYTES))

    # -- the pass and agent runs ---------------------------------------------------------------------------------------
    def run_once(self, *, scheduled: bool = False, only: str | None = None, agents: bool = True,
                 say: Callable[[str], None] | None = None, on: Callable[[Progress], None] | None = None) -> PassResult:
        """One pass: list, extract, link, compile. See `runner`. Its lines and its runs' transcripts come masked.
        `scheduled`, as `serve` runs it, checks each source only on its freshness window; otherwise every source is
        checked now, or the one `only` names. With `agents` false, no agent runs. `say` is handed a masked line as
        each stage starts and as each agent's run ends, and `on` each event of the pass, its words masked too."""
        self.files_read()
        if only is not None and only not in self.connectors:
            raise WorkspaceError(f"there is no source {only}: name one of {', '.join(sorted(self.connectors)) or 'none'}")
        self.refresh_mask()
        told = None if say is None else (lambda line: say(self.mask_every()(line)))
        heard = None if on is None else (lambda event: on(replace(event, key=self.mask_every()(event.key),
                                                                   text=self.mask_every()(event.text),
                                                                   why=self.mask_every()(event.why))))
        result = run_pass(self, scheduled=scheduled, only=only, agents=agents, say=told, on=heard)
        result.failed = {k: self.mask_every()(v) for k, v in result.failed.items()}
        result.ran = {k: self.mask_every()(v) for k, v in result.ran.items()}
        result.skipped = {k: self.mask_every()(v) for k, v in result.skipped.items()}
        result.refused = [self.mask_every()(line) for line in result.refused]
        result.notices = [self.mask_every()(line) for line in result.notices]
        result.warnings = [self.mask_every()(line) for line in result.warnings]
        for run in result.runs:
            self._mask_run(run)
        return result

    def hold(self, agent: Agent) -> Any:
        """A block that holds the agent's lease, as a pass does, for a run outside one: renewed while it runs and given
        back after. Refused in one line when another process runs that agent."""
        return runner.hold(self, agent)

    def ready_by_hand(self, agent: Agent, articles: list[str]) -> list[str]:
        """Before a run by hand, birth a fixed agent's article and link to it the facts the wiki holds, as a pass
        does before a due run. Gives a line for each fact that failed to link."""
        return [self.mask_every()(line) for line in runner.ready_by_hand(self, agent, articles)]

    def compile(self, agent: Agent, this: str | None = None, scheduled: bool = False, only: str = "",
                hold: Callable[[list[str]], str] | None = None, by_hand: bool = False) -> Compiled:
        """Run one agent on `this`, the article it writes, if anything it reads has moved or the run is scheduled.
        `by_hand` is a person's ask, as `compile --article`: it runs what moved, a failed run's wait or not.
        `compile.targets` lists the articles an agent writes. Its runs' transcripts and its lines come masked. With
        no key for its model, it is refused in one line before anything runs. `hold` is the cooldown, as
        `compile_article` takes it. A run that lands, or that ran and wrote nothing, moves the article's clock, by hand
        or in a pass; one that fails does not. What grounding did on it is counted in its `grounding`."""
        self.files_read()
        self.need_key(agent)
        self.refresh_mask()
        began = self.clock()
        tally: Counter[str] = Counter()  # what grounding does while it compiles
        counting = TALLY.set(tally)
        try:
            with masking(self.mask_every()):  # what a source gives a compile is masked before a line that says it is cut
                compiled = compile_article(agent, lookup=self.lookup, store=self.store, agent_run=self.agent_run,
                                           root=self.root, functions=self.exposed.evidence,
                                           agent_tools=self.exposed.agent_tools, workspace=self.handle, ledger=self.ledger,
                                           model=self.model_of(agent), clock=self.clock, this=this, subagents=self.agents,
                                           scheduled=scheduled, decider=self.decider, decision=self.models["decision"],
                                           zone=self.zone, starter=self.start_script, sources=self.connectors,
                                           descriptions=descriptions(self.settings), only=only, hold=hold,
                                           by_hand=by_hand, min_grounding=min_grounding(self.settings))
        finally:
            TALLY.reset(counting)
        compiled.grounding = dict(tally)
        if compiled.article is not None:
            if not compiled.skipped and (compiled.run is not None or compiled.runs):
                runner.ran_on(self, agent, compiled.article.identity.id, began)
        elif not compiled.skipped and (shown := runner._shown(compiled)) is not None and shown.status == WROTE_NOTHING:
            runner.ran_on(self, agent, this or agent.target, began)  # as in a pass, a run that wrote nothing moves it
        for run in {id(r): r for r in [*compiled.runs, *([compiled.run] if compiled.run else [])]}.values():
            self._mask_run(run)
        compiled.warning = self.mask_every()(compiled.warning)
        compiled.dropped = [self.mask_every()(line) for line in compiled.dropped]
        compiled.cleaned = [self.mask_every()(line) for line in compiled.cleaned]
        compiled.notes = [self.mask_every()(line) for line in compiled.notes]
        return compiled

    def curate(self, agent: Agent, changed_only: bool = False, wake: str = "hand",
               accepted: Collection[str] = ()) -> Curated:
        """Run one curator on its collection; with `changed_only`, only if what it reads moved. `wake` is what woke
        it, as `curate.run_curator` takes it. What it gives comes masked. With no key for its model, it is refused in
        one line before anything runs."""
        self.need_key(agent)
        self.refresh_mask()
        curated = run_curator(agent, lookup=self.lookup, store=self.store, agent_run=self.agent_run, root=self.root,
                              functions=self.exposed.evidence, agent_tools=self.exposed.agent_tools, workspace=self.handle,
                              ledger=self.ledger, model=self.model_of(agent), clock=self.clock,
                              changed_only=changed_only, starter=self.start_script, sources=self.connectors,
                              decider=self.decider, decision=self.models["decision"], wake=wake,
                              accepted=accepted)
        return self.masked(curated)

    def weighs_nothing(self, agent: Agent) -> bool:
        """Whether a curator has nothing to propose from, so a first-run round does not ask it: see
        `curate.nothing_to_weigh`."""
        return nothing_to_weigh(agent, lookup=self.lookup, store=self.store, functions=self.exposed.evidence,
                                workspace=self.handle, root=self.root, clock=self.clock, sources=self.connectors,
                                ours=read_ours(self.root))

    def files_read(self) -> None:
        """Read `workspace.md` and `profile.md` again, as a pass, a compile and `status` do first, and raise the fault
        of either that does not read, in one line. A lookup goes on without them."""
        read_fiscal(self.root)
        self._knows_ours()

    def _knows_ours(self) -> None:
        """Read who is yours from `profile.md` and `workspace.md` as they stand, before a source lists or gets a
        record: raise the fault of either while it stands, so no source lists with an own side that could not be read,
        and when what they say changed, hand it to every source. A fault leaves the side read before as it was, never
        no one."""
        try:
            ours = read_ours(self.root)
        except WorkspaceError as error:
            self._unread = str(error)
            raise
        if ours == self.ours and not self._unread:
            return
        self.ours, self._unread = ours, ""
        for connector in self.connectors.values():
            getattr(connector, "inner", connector).take_ours(ours)

    # -- models and their keys -----------------------------------------------------------------------------------------
    def model_of(self, agent: Agent) -> ModelSpec:
        """The model an agent runs on: its file's `model:`, or the compilation model of settings."""
        return read_model(agent.model, f"agents/{agent.id}.md model") if agent.model else self.models["compilation"]

    def need_key(self, agent: Agent) -> None:
        """Refuse a run of `agent` before it starts when the workspace runs its own model and holds no key for it."""
        if not self._own_run:
            return
        spec = self.model_of(agent)
        if not self.model_key(spec)():
            raise MissingModelKey(f"agents/{agent.id}.md runs on {spec.provider} {spec.model}, and no key for it is "
                                  f"kept or set: run cerebro secrets set {spec.key_name}, or export {spec.variable}")

    def model_key(self, spec: ModelSpec) -> Callable[[], str]:
        """A function that reads a model's key when called: the secrets store first, under the provider's id, then
        the provider's environment variable. Each value read is masked from then on. A provider whose calls go to
        a `base_url` of settings reads `NO_KEY`, and its own key is never sent there."""
        if self.gateway.route(spec.provider).base_url:
            return lambda: NO_KEY
        return model_key(self.secrets, spec, seen=self.mask.add)

    def _compile_key(self, provider: str) -> str:
        """A model's key by its provider's name, for a compile or for the facts layer: the secrets store first, then
        the environment."""
        return self.model_key(ModelSpec(provider, ""))()

    def _header_secret(self, name: str) -> str:
        """A header's value the secrets store keeps under `name`, for a model call, masked from then on."""
        if self.secrets is None:
            raise SecretsError(f"a header under providers in settings.yaml takes the secret {name}, and settings.yaml "
                               f"has no id, so no secrets store is open: add id: to settings.yaml, then run cerebro "
                               f"secrets set {name}")
        try:
            value = self.secrets.get(name)
        except SecretNotFound:
            raise SecretNotFound(f"a header under providers in settings.yaml takes the secret {name}, and none is "
                                 f"kept: run cerebro secrets set {name}") from None
        self.mask.add(value)
        return value

    def _decider(self) -> Decider | None:
        """The decision model's client, built from settings and its key, or None with one notice that says why."""
        spec = self.models.get("decision")
        if spec is not None and PROVIDERS[spec.provider].decision_url:
            key = self.model_key(spec)
            if key():
                return HttpDecider(spec, key=key, route=self.gateway.route(spec.provider),
                                   mask=lambda text: self.mask_every()(text))
            why = (f"models.decision has no key: {spec.key_name} is not in the secrets store and {spec.variable} is "
                   "not set")
        else:
            why = "settings.yaml names no decision model with a decision API"
        self.notices.append(f"{why}, so grounding scores no sentence and the near-match judge runs on the words "
                            "alone: set the key, or name a decision model")
        return None

    def _reranker(self, extraction: Extraction | None) -> DecisionReranker | None:
        """What re-ranks the best hits of `matching` on the decision model, when `search: { rerank: true }` asks for
        it and there is a decision model, else None, with a notice when it was asked for."""
        asked = read_rerank(self.settings)
        if not asked.on:
            return None
        if self.decider is None:
            self.notices.append("search.rerank is on, but this workspace has no decision model to re-rank with, so "
                                "matching keeps its search order: set the decision model's key, or turn rerank off")
            return None
        return DecisionReranker(self.decider.bounded(RERANK_CALL_SECONDS), store=self.store, extraction=extraction,
                                ledger=self.ledger, decision=self.models["decision"], clock=self.clock, root=self.root,
                                top=asked.top, masks=self.mask_every)

    def asking(self) -> Ask:
        """The workspace's extraction model, as a connector asks it to group what it offers: `ask_model` to ask, and
        `ask_room` for the room a request has beside the prompt."""
        return Ask(self.ask_model, self.ask_room)

    def _system(self, prompt: str) -> str:
        """The prompt named `prompt`, its `$workspace` filled from `workspace.md`."""
        return prompts.fill(prompt, self.root, workspace=_workspace(self.root) or "(workspace.md says nothing)")

    def ask_room(self, prompt: str) -> int:
        """How many bytes a request may hold beside the prompt named `prompt`, filled, in one call to the extraction
        model: `EXTRACTION_INPUT_BYTES` less the prompt's."""
        return EXTRACTION_INPUT_BYTES - len(self._system(prompt).encode())

    def ask_model(self, prompt: str, request: str, job: str = "extraction") -> str:
        """One question to the workspace's model of `job`, the extraction model as a connector asks it to group what
        it offers, or the compilation model as a list of proposals is arranged: the bundled prompt named `prompt`, or
        the workspace's ejected copy, with `$workspace` filled from `workspace.md`, and the `request`. Refused in one
        line, with nothing sent, when the model has no key, or when a question to the extraction model passes
        `EXTRACTION_INPUT_BYTES`. Held to the job's layer's cap try by try, as `budget.Held` holds a call, under the
        prompt's name. `asker` stands in for the model, as a test's does. The
        reply's text."""
        system = self._system(prompt)
        spec = self.models[job]
        if job == "extraction" and len((system + request).encode()) > EXTRACTION_INPUT_BYTES:
            raise CerebroError(f"the {prompt} prompt and its question do not fit in one call to the extraction model")
        if self.asker is None and not self.model_key(spec)():
            raise MissingModelKey(f"the {job} model, {spec.provider} {spec.model}, has no key: run cerebro "
                                  f"secrets set {spec.key_name}, or export {spec.variable}")
        # a list's arrangement names every item once, and a model that reasons thinks from the same room
        asker = self.asker or PydanticRewriter.from_spec(spec, key=self._compile_key, masks=self.mask_every,
                                                         what=prompt, gateway=self.gateway,
                                                         max_tokens=32_000 if job == "compilation" else None)
        return Held(asker, self.ledger, job, spec, self.clock, prompt).rewrite(system, request)[0]

    def prove_key(self, job: str, spec: ModelSpec, value: str) -> None:
        """Prove a key not yet kept with one small call on `spec`, the model of `job`, held to the day's budget try by
        try as `budget.Held` holds a call, so a model with no known price is refused under a daily cap before any call,
        with the runtime's line, and what it spent recorded under `keys.PROOF`. A decision model is asked one yes or no on its decision
        API; any other model is asked to reply with a word. `asker` stands in for the model, as a test's does. The
        value is masked from then on. Raises with why it did not verify."""
        from .keys import PROOF

        self.mask.add(value)
        value = NO_KEY if self.gateway.route(spec.provider).base_url else value  # a gateway gets no provider's key
        layer = "extraction" if job == "extraction" else "compilation"
        if self.asker is None and spec.job == "decision" and PROVIDERS[spec.provider].decision_url:
            asked = HttpDecider(spec, key=lambda: value, route=self.gateway.route(spec.provider), call=PROOF)
            Held(asked, self.ledger, layer, spec, self.clock, PROOF).decide(
                "The sky is above the ground.", {"proof": YesNo("Is the sky above the ground?")})
            return
        asker = self.asker or PydanticRewriter.from_spec(spec, key=lambda provider: value, masks=self.mask_every,
                                                         what=PROOF, gateway=self.gateway)
        try:
            Held(asker, self.ledger, layer, spec, self.clock, PROOF).rewrite("Reply with the word ok.", "ok")
        except RewriteFailed as error:
            cause = error.__cause__  # the provider's reply may echo the key, which no mask holds before it is kept
            if cause is None:
                raise
            status = getattr(cause, "status_code", None)
            raise RewriteFailed(f"the {PROOF} call failed: {type(cause).__name__}"
                                + (f" (status {status})" if status is not None else "")) from None

    # -- building connectors -------------------------------------------------------------------------------------------
    @property
    def http(self) -> httpx.Client:
        """The one HTTP client every connector's requests go through, opened when first asked for."""
        if self._http is None:
            self._http = httpx.Client(timeout=30.0, follow_redirects=False, verify=_tls())
            self._own_http = True
        return self._http

    def login(self, source: SourceFile) -> Callable[[], str]:
        """A function that reads a source's login from the secrets store when called, and masks it from then on. With
        no store open, calling it fails with one line that says what to add."""
        name = login_name(source) or source.name

        def read() -> str:
            if self.secrets is None:
                raise SecretsError(f"sources/{source.name}.yaml needs a login, and settings.yaml has no id, so no "
                                   f"secrets store is open: add id: to settings.yaml, then run cerebro connect "
                                   f"{source.name}")
            try:
                value = login_getter(self.secrets, source)()
            except SecretNotFound:
                raise SecretNotFound(f"sources/{source.name}.yaml needs a login, and none is kept as {name}: run "
                                     f"cerebro connect {source.name}") from None
            self.mask.add(value)
            return value

        return read

    def token(self, source: SourceFile) -> Callable[[], str]:
        """A source's token, read as `login` reads it, checked each time it is about to be sent: a value that is a
        whole browser sign-in record, refresh token and all, is never sent as a token."""
        read = self.login(source)

        def token() -> str:
            value = read()
            if is_login(value):
                raise TokenIsLogin(f"sources/{source.name}.yaml: the secret {login_name(source)} holds a browser "
                                   f"sign-in, not a token, so it is not sent: write auth: oauth to use the sign-in, "
                                   f"or keep a token with cerebro connect {source.name} --replace")
            return value

        return token

    def bad_auth(self, source: SourceFile, item: str) -> SourceError:
        return SourceError(f"sources/{source.name}.yaml auth {source.auth}: write auth: oauth to sign in through the "
                           f"browser with cerebro connect {source.name}, or auth: {{ keychain: {item} }} to keep a "
                           f"token")

    def signed_in(self, source: SourceFile, server: str) -> Credentials:
        """The credentials of a source whose file says `auth: oauth`: a live token from the browser login `cerebro
        connect` kept, renewed as it expires, sent to `server` alone."""
        if self.secrets is None:
            raise SourceError(f"sources/{source.name}.yaml auth oauth: settings.yaml has no id, so no secrets store "
                              f"is open: add id: to settings.yaml, then run cerebro connect {source.name}")
        return LiveToken(self.secrets, sign_in_name(source), server, http=self.http, now=self.clock,
                         source=source.name, seen=self.mask.add)

    def mcp_session(self, source: SourceFile, url: str, tools: tuple[str, ...]) -> Callable[..., Any]:
        """A `call` for a first-party connector over MCP: the server at `url`, reached with the source's token from
        the secrets store, through a session opened on the first call and kept until `close`, that calls `tools`
        alone."""
        oauth = source.auth == "oauth"
        if not oauth and source.auth not in ("", "token", "api_key") and not source.auth.startswith("keychain:"):
            raise self.bad_auth(source, login_name(source) or source.kind)
        if source.command:
            raise SourceError(f"sources/{source.name}.yaml command: a {source.kind} source reads "
                              f"{source.kind}'s own server: delete command")
        given = source.url or url
        if urlsplit(given).hostname != urlsplit(url).hostname or not given.startswith("https://"):
            raise SourceError(f"sources/{source.name}.yaml url {urlsplit(given).hostname or given}: it is not "
                              f"{source.kind}'s server, so the {source.kind} login is not sent there: give {url} "
                              f"or leave url out")
        token = self.signed_in(source, given) if oauth else self.token(source)
        where = replace(source, url=given, auth="token")

        def call(tool: str, arguments: dict[str, Any] | None = None) -> Any:
            with self._mcp_lock:
                if source.name not in self._mcp:
                    self._mcp[source.name] = self.mcp_open(reach(where, self.root, credentials=token))
                session = self._mcp[source.name]
            return session.reader(*tools)(tool, arguments)

        return call

    def own_files(self) -> list[Path]:
        """The workspace's own files, which no source may read: its state folder, the store's file wherever settings
        move it, and the facts layer's project and data."""
        own = [self.root / ".cerebro", self.root / FACTS_STATE, self.root / "facts"]
        block = self.settings.get("store")
        block = block if isinstance(block, dict) else {}
        path = block.get("path", DEFAULT_PATH) if block.get("class", "sqlite") == "sqlite" else DEFAULT_PATH
        if isinstance(path, str) and path.strip():
            file = Path(path).expanduser()
            own.append(file if file.is_absolute() else self.root / file)
        return own

    def admitted(self, key: str) -> list[str]:
        """The values an identity key holds across the articles the store holds as admitted: every active article, in
        any collection, read when a listing asks. The articles are `curate.working_set`, the set `curate.in_scope`
        extracts by."""
        return sorted({value for who in working_set(self.store) for value in who.keys.get(key, [])})

    # -- the six operations --------------------------------------------------------------------------------------------
    def find(self, caller: Caller, name: str) -> FindResult:
        return self.lookup.find(caller, name)

    def query(self, caller: Caller, source: str, filters: dict[str, Any] | None = None, **options: Any) -> QueryResult:
        """See `Lookup.query` for `where`, `matching`, `order_by`, `descending`, `limit`, and `offset`."""
        return self.lookup.query(caller, source, filters, **options)

    def read(self, caller: Caller, ids: list[str] | str, about: str | None = None) -> ReadResult:
        """Articles whole, each with the facts learned since it was last written, and facts asked for by id. `about`
        brings an article's linked facts that hold its words. An article's other facts are read by their ids."""
        return self.lookup.read(caller, ids, about, linked=False)

    def tell(self, caller: Caller, text: str, target: str | None = None, as_: str | None = None,
             kind: str | None = None) -> TellResult:
        return self.masked(self.lookup.tell(caller, text, target, as_, kind))

    def decide(self, caller: Caller, accept: list[str | Decision] | None = None,
               reject: list[str | Decision] | None = None) -> DecideResult:
        """Accept or reject proposals. A decision on what one curator alone filed is its act, and no news to it."""
        ids = {(d if isinstance(d, str) else d.id).strip() for d in [*(accept or []), *(reject or [])]}
        filed = {p.by for p in proposals(self.store) if p.id in ids}
        curator = next((a for a in self.agents if a.role == "curator" and filed == {Caller(a.id, "agent")}), None)
        if curator is None:
            return self.masked(self.lookup.decide(caller, accept, reject))
        return self.masked(decided(curator, self, caller, accept, reject))

    def retract(self, caller: Caller, id: str, reason: str = "") -> RetractResult:
        return self.masked(self.lookup.retract(caller, id, reason))

    def review(self, caller: Caller | None = None) -> list[Review]:
        """The proposals waiting for `caller`, a person when none is given, each with what accepting it changes. A
        person is not asked about one a curator holds, as `with_curators` gives them."""
        found = review(self.store, caller, extraction=self.extraction, root=self.root)
        cited = {id for item in found for id in item.proposal.cites}  # each waits with the act that names it
        found = [item for item in found if item.proposal.id not in cited]
        if caller is None or caller.kind == "person":
            held = {p.id for p in self.with_curators()}
            found = [item for item in found if item.proposal.id not in held]
        return self.masked(found)

    def awaiting(self) -> int:
        """How many waiting proposals a person is asked about, as `review` lists them."""
        waiting = proposals(self.store, "waiting")
        named, held = cited(waiting), {p.id for p in self.with_curators(waiting)}
        return sum(p.id not in named and p.id not in held for p in waiting)

    def with_curators(self, waiting: list[Proposal] | None = None) -> list[Proposal]:
        """The waiting proposals, of `waiting` when given, that another curator filed and their collection's curator
        may answer: never one a person decides, as `for_a_person` gives them."""
        waiting = proposals(self.store, "waiting") if waiting is None else waiting
        named = cited(waiting) | for_a_person(self.store, waiting)
        curators = [a for a in self.agents if a.role == "curator"]
        return [p for p in waiting if any(may_answer(a, p, named) for a in curators)]

    def call(self, caller: Caller, name: str, args: dict[str, Any]) -> Any:
        """One of the six operations, or an app's MCP tool, by its MCP name, with its arguments as an outside agent
        sends them: `query` takes `from` and field keys, and `tell` takes `as`. Gives the operation's data as an
        outside agent gets it, as `reply.outside` shapes it. An operation the workspace does not offer raises
        `NotImplementedError` with one line that says so."""
        return outside(self._call(caller, name, args))

    def _call(self, caller: Caller, name: str, args: dict[str, Any]) -> Any:
        from pydantic import ValidationError  # pydantic and the MCP layer load only once a tool is called

        from .mcp import QueryArguments, TellArguments

        try:
            if name == "find":
                _takes(name, args, "name", need=("name",))
                return self.find(caller, args["name"])
            if name == "query":
                shaped = QueryArguments.model_validate(args).model_dump_one_level()
                return self.query(caller, shaped["source"], shaped["filters"], **shaped["options"])
            if name == "read":
                _takes(name, args, "ids", "about", need=("ids",))
                return self.read(caller, args["ids"], args.get("about"))
            if name == "tell":
                told = TellArguments.model_validate(args).model_dump_one_level()
                return self.tell(caller, told["text"], told["target"], told["lane"], told["kind"])
            if name == "decide":
                _takes(name, args, "accept", "reject")
                return self.decide(caller, _decisions(name, args.get("accept")), _decisions(name, args.get("reject")))
            if name == "retract":
                _takes(name, args, "id", "reason", need=("id",))
                return self.retract(caller, args["id"], args.get("reason", ""))
            if name in self.exposed.mcp_tools:
                mask = self.mask_every()
                try:
                    reply = bind(self.exposed.mcp_tools[name], caller, self.handle)(**args)
                except ValidationError:
                    raise  # refused below by where and why, never by the value it was handed
                except Exception as error:  # what an app's tool says is masked before any caller reads it
                    if (safe := masked_error(error, mask)) is error:
                        raise
                    raise safe from None
                return masked(reply, mask)
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ())) or "the arguments"
            raise OperationError(f"{name}: {where}: {first.get('msg', 'does not fit')}: fix that key") from None
        except TypeError as error:
            raise OperationError(f"{name}: {error}: send the keys the tool takes") from None
        except NotImplementedError as error:
            raise NotImplementedError(str(error) or f"{name} is not offered here: use find, query or read") from None
        tools = ["find", "query", "read", "tell", "decide", "retract", *self.exposed.mcp_tools]
        raise OperationError(f"there is no tool {name}: use one of {', '.join(tools)}")

    # -- what the workspace holds, as data for a surface to print ------------------------------------------------------
    def agent(self, id: str) -> Agent | None:
        """The agent with this id, the path of its file under `agents/` without `.md`, or None."""
        return next((agent for agent in self.agents if agent.id == id), None)

    def writer_of(self, article: str) -> Agent | None:
        """The agent that writes this article: the one that writes it alone, else the one that writes each article of
        its collection. None when no agent writes it."""
        collection = article.rpartition("/")[0]
        alone = [a for a in self.agents if a.role == "writer" and a.target == article]
        each = [a for a in self.agents if a.role in ("article", "composer", "query") and a.target == collection]
        return next(iter(alone + each), None)

    def agent_list(self) -> dict[str, Any]:
        """Each agent that loaded, with its role, target, heartbeat and the status of its last run, and a line for
        each file that did not load."""
        last = {row.data.get("agent"): row.data for row in self.store.latest("agent-runs", key="agent")}
        rows = [{"id": a.id, "role": a.role, "target": a.target, "heartbeat": list(a.heartbeat),
                 "path": f"agents/{a.id}.md", "last_run": last.get(a.id, {}).get("status")} for a in self.agents]
        return {"agents": rows, "refused": [line for line in self.refused if line.startswith("agents/")]}

    def status(self) -> dict[str, Any]:
        """What is in the workspace, as `cerebro status` shows it: its agents, sources, articles and facts, what waits
        for a person, today's cost against its caps, and what did not load or has no key, read as a run reads it."""
        from .scaffold import missing_keys

        self.files_read()
        you = Caller("you", "person")
        now = self.clock()
        caps = self.ledger.caps
        waits = [a.identity for c in self.store.collections() for a in self.store.query(c) if pending(a)]
        counted = {c: self.lookup.query(you, c, limit=0).count for c in self.store.collections()}
        waiting = {c: sum(1 for who in waits if who.collection == c) for c in counted}
        articles = {c: n - waiting[c] for c, n in counted.items() if n - waiting[c]}  # the written: pending is apart
        needed = {**self.models, **{f"facts {n}": spec for n, spec in enumerate(self._needs())}}
        last = {row.data.get("agent"): row.data for row in self.store.latest("agent-runs", key="agent")}
        return {
            "root": str(self.root),
            "agents": len(self.agents),
            "sources": sorted(self.connectors),
            "facts_layer": self.extraction is not None,
            "facts": self._facts_status(),
            "last_runs": {a.id: {k: last[a.id].get(k) for k in ("status", "ended_at", "model", "tool_calls", "note")}
                          for a in self.agents if a.id in last},
            "articles": articles,
            "pending": [who.id for who in waits],
            "spent_today": {"extraction": self.ledger.spent(now, "extraction"),
                            "compilation": self.ledger.spent(now, "compilation"),
                            "evaluation": self.ledger.spent(now, "evaluation"),
                            "rerank": self.ledger.spent(now, "rerank"), "total": self.ledger.spent(now)},
            "caps": {"extraction": caps.extraction, "compilation": caps.compilation, "evaluation": caps.evaluation,
                     "rerank": caps.rerank, "total": caps.total},
            "rerank": self._rerank_status(),
            "refused": list(self.refused),
            "compile_order": runner.collection_order(self.store, self.agents, self.store.collections()).line(),
            "notices": list(self.notices),
            "missing_keys": missing_keys(needed, lambda spec: self.model_key(spec)()),
            "missing_logins": self._missing_logins(),
            "kinds": self.class_notices(),
            "checks": self.checks(),
            "working_set": len(working_set(self.store)),
            "waiting": self.awaiting(),
            "tells_waiting": len(waiting := told_waiting(self.store)),
            "tells_waiting_ids": waiting,
            "instructions": [{"article": p.target, "by": p.by.name, "kind": p.by.kind, "at": p.at,
                              "text": str(p.payload.get("text", ""))}
                             for p in sorted((p for p in proposals(self.store, "accepted")
                                              if p.kind == "article.instruct"),
                                             key=lambda p: p.by.kind == "agent")],  # a person's first
            "expires": [{"id": item.proposal.id, "target": item.proposal.target, "title": item.title,
                         "reason": item.proposal.reason, "by": item.proposal.by.name}
                        for item in self.review(you) if item.proposal.kind == "record.expire"],
        }

    def _facts_status(self) -> dict[str, Any]:
        """The current facts, how many records gave them and by source, and, when the facts layer's checks dropped any
        candidate facts, how many, from how many records, and why, the commonest reason first."""
        held = self.extraction.facts() if self.extraction is not None else []
        by_source: dict[str, int] = {}
        for fact in held:
            by_source[fact.source or "unknown"] = by_source.get(fact.source or "unknown", 0) + 1
        facts: dict[str, Any] = {"current": len(held), "records": len({(f.source, f.record) for f in held}),
                                 "by_source": dict(sorted(by_source.items()))}
        if dropped := self.extraction.dropped() if self.extraction is not None else {}:
            why: dict[str, int] = {}
            for row in dropped.values():
                for name, n in (row.get("why") or {}).items():
                    why[name] = why.get(name, 0) + int(n)
            facts["dropped"] = {"candidates": sum(int(row.get("candidates") or 0) for row in dropped.values()),
                                "records": len(dropped), "why": dict(sorted(why.items(), key=lambda kv: -kv[1]))}
        return facts

    def _rerank_status(self) -> dict[str, Any]:
        """Whether `matching` re-ranks its best hits: asked for in settings, and running on a decision model."""
        asked = read_rerank(self.settings)
        on = self.lookup.rerank is not None and asked.on
        return {"asked": asked.on, "on": on, "top": asked.top,
                "model": self.models["decision"].model if on and self.models.get("decision") else ""}

    def _missing_logins(self) -> list[str]:
        """The sources whose kind needs a login that the secrets store does not hold, which `cerebro connect` gets."""
        from .connect import ConnectError, need

        missing = []
        for name in sorted(self.connectors):
            path = self.root / "sources" / f"{name}.yaml"
            try:
                needed = need(read_source(path)) if path.is_file() else None
            except (ConnectError, SourceError):
                continue
            if needed is None or needed.way in ("none", "cli") or not needed.name:
                continue
            try:
                held = self.secrets is not None and bool(self.secrets.get(needed.name))
            except SecretNotFound:
                held = False
            if not held:
                missing.append(name)
        return missing

    def _needs(self) -> list[ModelSpec]:
        """The models the facts layer calls beyond the extraction model, as it says."""
        needs = getattr(self.extraction, "needs", None)
        return list(needs()) if callable(needs) else []

    def class_notices(self) -> list[str]:
        """A line for each agent file whose facts evidence filters on a kind of fact the facts layer does not give,
        as `classes: [<kind>]`: such a line reads nothing, which looks like a quiet week. A layer that does not
        say its kinds is judged by the kinds its current facts carry. A line too for each kind facts carry that the
        workspace no longer defines, as `facts.kind_notices` says. None with no facts layer."""
        if self.extraction is None:
            return []
        held: dict[str, int] = {}
        for fact in self.extraction.facts():
            held[fact.kind] = held.get(fact.kind, 0) + 1
        wanted: dict[str, list[str]] = {}
        for agent in self.agents:
            kinds: list[str] = []
            for line in agent.evidence:
                args = line.args if isinstance(line.args, dict) else {}
                if line.kind == "facts" or (line.kind == "query" and args.get("from") == "facts"):
                    for key in ("classes", "kind"):
                        value = args.get(key)
                        kinds += [str(v) for v in (value if isinstance(value, list) else [value] if value else [])]
            wanted[f"agents/{agent.id}.md"] = kinds
        return kind_notices(wanted, self.extraction.kinds(), held)

    def checks(self) -> dict[str, dict[str, Any]]:
        """Each listing source's last check: when, whether it finished, and when it is next due under `serve`."""
        return runner.checks(self)

    def cursors(self) -> dict[str, dict[str, Any]]:
        """Each listing source's cursor: whether it holds one, whether it lists only what changed since one, and
        when it was last listed in full, as `runner.fulls` gives them."""
        return runner.fulls(self)

    def mcp_server(self) -> Any:
        """The workspace's MCP server: the six tools, and each function an app marked with `@mcp_tool`."""
        from .mcp import server

        return server(self)

    # -- tokens for the HTTP server ------------------------------------------------------------------------------------
    def make_token(self, name: str) -> Any:
        """A new token for the caller `name`: a `tokens.NewToken`, whose plain token is shown this once. The
        workspace keeps only its hash."""
        return Tokens(self.store, self.clock).make(name)

    def tokens(self) -> list[Any]:
        """The live tokens, as `tokens.Token`: names, when made and last used. Never a token or its hash."""
        return Tokens(self.store, self.clock).live()

    def revoke_token(self, name: str) -> Any:
        """End the token of `name`, as a `tokens.Token`: the next request that carries it is refused."""
        return Tokens(self.store, self.clock).revoke(name)

    def may_serve(self, host: str) -> bool:
        """Whether the HTTP server may bind `host`, refused for an address other than loopback while the workspace has no
        live token. True when a live token exists, so a caller can get in at all."""
        return Tokens(self.store, self.clock).may_serve(host)
