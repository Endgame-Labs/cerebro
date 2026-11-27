"""Cerebro's seam to its facts layer: what a layer gives back, the layer on Factoid, and the earlier saves kept.

A facts layer, an `Extraction`, turns each whole record into facts. Factoid's is the one that ships, as
`FactoidExtraction`, and this is the only module that imports Factoid. A layer knows nothing of articles: a fact
carries its subject, and the `identity` module links facts to articles. `Revisions` wraps a layer so that what an
earlier save of a record said stays readable once a later save replaces it.

A record becomes two Factoid documents. Its text is one, at the uri `cerebro:<source>/<record>`, which fixes the
document's id so that a record sent again replaces its facts whole. Its fields are the other, at
`cerebro:<source>/<record>#fields`: one `field_value` fact a field, "The phase of Alder is done.", which Factoid
writes by rule with no extraction call. A value sent again is the fact it holds and a changed one replaces it, and
each new one still costs one decision call, for its time properties, and an embedding. A document's title is a
plain name a person knows the record by, since Factoid's models read it: which source and record a document holds
is kept beside Factoid's data, in `origins.json`, which no model reads.

Kinds of fact: a workspace defines its own as Factoid `Fact` subclasses, in Python files under its `facts/` folder,
and a workspace that defines none has the one kind `fact`. A kind's docstring says what a fact of that kind states,
and Factoid's decision model reads it to give each fact its kind; an optional `guidance` is read by the extractor.
A kind's name is its class name in snake case, and is what an agent file filters on. A fact keeps the kind it was
given: a record takes the kinds defined now when it is extracted again, and `kind_notices` says so.

Calling Factoid: every call into it runs inside `_active`, under one lock, since Factoid keeps one active app for
the whole process. Each model call reads its key as Cerebro reads keys, goes to the host the gateway names, and is
held to a few tries and a time. What the calls spent comes back with the facts, for the ledger.

Factoid keeps its data under the workspace's `.cerebro/factoid/`, and that holds each record's text: in its store,
its chunk index and its log of model calls.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import os
import re
import sys
import threading
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote

import httpx

from .clock import stamp, system_clock
from .connector import Record
from .errors import CerebroError
from .models import DEFAULTS, PROVIDERS, ModelSpec, advice_for
from .private import make_folder, owner_only, write_file
from .search import Hit

if TYPE_CHECKING:
    from .gateway import Gateway


__all__ = [
    "FACTOID_DECISION",
    "FIELD_KIND",
    "GUIDANCE",
    "KINDS",
    "REVISIONS_KEPT",
    "SETTINGS_KEYS",
    "Extracted",
    "Extraction",
    "ExtractionFailed",
    "Fact",
    "FactNotFound",
    "FactoidExtraction",
    "FactsSettingsError",
    "KindError",
    "Mention",
    "NoBody",
    "Quote",
    "Revisions",
    "context_of",
    "fact_from",
    "factoid_models",
    "field_rows",
    "field_sentence",
    "kind_name",
    "kind_notices",
    "load_kinds",
    "revision_of",
    "revisions_kept",
    "saved_revision",
    "superseded",
]


# -- the seam a facts layer implements ------------------------------------------------------------------------------

@dataclass
class Quote:
    """Words a fact rests on, as its record gives them, with who said them, when, and on which side of the
    conversation: `yours`, `theirs`, or empty when the record does not say. `passage` is the number of its passage
    in its record, counted from 1, so two quotes of one record whose numbers follow each other stand next to each
    other: 0 when not known. `record` is that record, the fact's own or one that stated it again: empty when not
    known."""

    text: str
    speaker: str = ""
    at: str = ""
    side: str = ""
    passage: int = 0
    record: str = ""


@dataclass
class Mention:
    """A thing a fact names: its words, its role in the fact, such as `about` or `speaker`, and the keys that
    identify it, such as an email."""

    text: str
    role: str = "about"
    keys: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Fact:
    """One fact: its text, what it is about, its kind, when it holds, and the record and quotes it rests on.

    `subject` is the words of the mention the fact is about. `status` is `current`, `retracted`, or `superseded`
    for a fact an earlier revision of its record gave and a later one no longer does. A superseded fact carries the
    `revision` it came from, and `superseded_at`, the time of the revision that replaced it, or when the wiki took
    that revision in if it gives no time. `field` is the field a `field_value` fact states, by its name in the
    record, and empty for any other fact.
    """

    id: str
    text: str
    subject: str
    kind: str
    at: str
    source: str
    record: str
    quotes: list[Quote] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    status: str = "current"
    revision: str = ""
    superseded_at: str = ""
    field: str = ""


@dataclass
class Extracted:
    """What one record gave: its facts, and the tokens the model calls behind them spent, so the extraction layer's
    spend can be priced and held to its cap."""

    facts: list[Fact]
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: the input and output tokens of each model that spent them, by its `provider/model`: the rest are the
    #: extraction model's
    models: dict[str, tuple[int, int]] = field(default_factory=dict)


class FactNotFound(CerebroError, LookupError):
    """No fact has that id."""


class NoBody(CerebroError, ValueError):
    """A record was handed over as a listing. Extraction needs the record whole."""


class ExtractionFailed(CerebroError, RuntimeError):
    """Factoid could not turn a record into facts, such as when a model call failed. The runner retries it later.
    `spent` is what its model calls cost before it failed, for the ledger."""

    def __init__(self, message: str, spent: Extracted | None = None):
        super().__init__(message)
        self.spent = spent or Extracted([])


class FactsSettingsError(CerebroError, ValueError):
    """The workspace's extraction model is one Factoid cannot call."""


class KindError(CerebroError, ValueError):
    """A file in the workspace's `facts/` folder does not define its kinds of fact soundly."""


class Extraction:
    """The seam a facts layer implements."""

    def extract(self, record: Record) -> Extracted:
        """Turn one whole record into facts, replacing the facts an earlier revision of it gave, and say what that
        spent. `Revisions` keeps what the earlier revision gave."""
        raise NotImplementedError

    def extract_fields(self, record: Record) -> Extracted:
        """Turn the fields a record gives into facts, one a field, as `field_rows` and `field_sentence` say, apart
        from what `extract` makes of its text: a value given again is the fact already held, and a changed one
        replaces it. A record that gives no field gives none. A layer that reads no fields gives none."""
        return Extracted([])

    def facts(self, *, ids: list[str] | None = None, source: str | None = None, record: str | None = None,
              since: str | None = None, kinds: list[str] | None = None, limit: int | None = None,
              latest: bool = True) -> list[Fact]:
        """The current facts kept, newest first by `at`, that meet every filter given. `since` keeps facts at or
        after it. Facts asked for by `ids` come whatever their status. With `latest` false, the superseded facts of
        earlier revisions come too, where the layer keeps them: a layer on its own keeps none, and `Revisions` does."""
        raise NotImplementedError

    def retract(self, fact_id: str) -> None:
        """Retract one fact: it stays readable by its id, with status `retracted`, and leaves every other read."""
        raise NotImplementedError

    def search(self, text: str, top_k: int = 10, *, source: str | None = None) -> list[Hit]:
        """The current facts whose words match `text`, best first, as hits by fact id."""
        raise NotImplementedError

    def search_spent(self, text: str, top_k: int = 10, *, source: str | None = None) -> tuple[list[Hit], Extracted]:
        """`search`, and the tokens its model calls spent, as an `Extracted` with no facts. A layer whose search calls
        no model spends none."""
        return self.search(text, top_k, source=source), Extracted([])

    def kinds(self) -> list[str]:
        """The kinds of fact this layer gives, by the names an agent file filters on, so a filter on a kind it never
        gives can be named before it reads nothing. A layer that gives one kind gives `fact`. Empty when the layer
        cannot say ahead of time, as when its kinds come with its records: its facts' kinds are judged instead."""
        return ["fact"]

    def needs(self) -> list[ModelSpec]:
        """The models this layer calls beyond the workspace's extraction model, each with the key it reads, so `init`
        and `status` can name every key a pass needs. A layer that calls no other model needs none."""
        return []

    def sent(self, record: Record) -> str:
        """The text this layer sends its models to extract `record`, which the cap's hold is reckoned from: its body,
        unless the layer sends more."""
        return record.body or ""

    def dropped(self) -> dict[str, dict[str, Any]]:
        """The candidate facts this layer's checks dropped from each record at its last extraction, by
        `<source>/<record>`: how many, as `candidates`, and how many failed each check, as `why`. Only records that
        lost any. A layer that drops nothing, or does not say, gives none."""
        return {}


def _meets(fact: Fact, source: str | None, record: str | None, since: str | None, kinds: list[str] | None) -> bool:
    """Whether `fact` meets a read's filters, as `Extraction.facts` takes them."""
    after = None if since is None else _utc(since) or since
    return ((source is None or fact.source == source) and (record is None or fact.record == record)
            and (after is None or fact.at >= after) and (kinds is None or fact.kind in kinds))


# -- earlier saves --------------------------------------------------------------------------------------------------

#: How many earlier saves of one record keep their facts, unless `settings.yaml` says `facts: revisions_kept:`.
REVISIONS_KEPT = 10
#: The state key under which the revision a record was last taken in at is kept, with when.
SAVED = "saved:"


class Revisions(Extraction):
    """A facts layer that keeps what earlier revisions of a record said, in the workspace's store.

    A handler holds each record at its latest revision: one sent again replaces the facts it gave, as Factoid
    retracts them. So when a record comes in at a revision other than the one it was last taken in at, its current
    facts are kept in the store as superseded first, before the handler is called, each with the revision it came
    from and when the new one replaced it: the time the new revision gives itself, or when it came in if it gives
    none. The revision taken in, with the record's own time, is written under `saved:<source>/<record>` only once the
    handler has given the new facts, so a save that fails at any step is tried again from where it began, and nothing a source said is lost.
    A record with no revision of its own is told by a hash of its body. A record taken in again at the revision it
    holds, as a full listing gives it, supersedes nothing. The one place old facts go: past `keep` earlier saves of
    one record, the oldest save's facts are dropped.

    Every read gives what the handler gives, so a superseded fact stays out of every query, link, search and count,
    unless a read asks for it: `facts(latest=False)` adds the superseded facts that meet its filters, and
    `facts(ids=...)` gives a superseded fact by its id, marked `superseded`. A superseded fact the latest save still
    gives, by id or in the same words, is that save's and is never read as an earlier one. A retract takes back a
    fact in every save: the handler's, and each superseded fact of its record in the same words. A retracted fact
    never becomes superseded. Every other call goes to the handler."""

    def __init__(self, handler: Extraction, store: Any, *, keep: int = REVISIONS_KEPT,
                 clock: Callable[[], datetime] | None = None):
        self.handler, self.store, self.keep, self.clock = handler, store, keep, clock or system_clock

    def __getattr__(self, name: str) -> Any:
        if name == "handler":  # not set yet, as while a copy is made
            raise AttributeError(name)
        return getattr(self.handler, name)

    # `Extraction` defines these, so `__getattr__` never sees them: each is sent on to the handler by name.
    def kinds(self) -> list[str]:
        return self.handler.kinds()

    def needs(self) -> list[ModelSpec]:
        return self.handler.needs()

    def dropped(self) -> dict[str, dict[str, Any]]:
        return self.handler.dropped()

    def sent(self, record: Record) -> str:
        return self.handler.sent(record)

    def extract_fields(self, record: Record) -> Extracted:
        return self.handler.extract_fields(record)

    def search(self, text: str, top_k: int = 10, *, source: str | None = None) -> list[Hit]:
        return self.handler.search(text, top_k, source=source)

    def search_spent(self, text: str, top_k: int = 10, *, source: str | None = None) -> tuple[list[Hit], Extracted]:
        return self.handler.search_spent(text, top_k, source=source)

    def extract(self, record: Record) -> Extracted:
        now = stamp(self.clock())
        revision = revision_of(record)
        earlier = saved_revision(self.store, record.source, record.id)
        if earlier != revision:
            before = self.handler.facts(source=record.source, record=record.id)
            if before:  # kept before the handler runs, which may retire them and then fail
                self.store.supersede(record.source, record.id, earlier, record.at or now,
                                     [asdict(f) for f in before], self.keep)
        extracted = self.handler.extract(record)
        if earlier != revision:
            self.store.set_state(f"{SAVED}{record.source}/{record.id}", {"revision": revision, "at": now,
                                                                         "dated": record.at or ""})
        return extracted

    def facts(self, *, ids: list[str] | None = None, source: str | None = None, record: str | None = None,
              since: str | None = None, kinds: list[str] | None = None, limit: int | None = None,
              latest: bool = True) -> list[Fact]:
        if ids is not None:
            given = self.handler.facts(ids=ids, source=source, record=record, since=since, kinds=kinds)
            kept = {f.id: f for f in self._kept(self.store.superseded(ids=list(ids)))}
            found = [f if f.status == "current" else kept.pop(f.id, f) for f in given]
            found += [f for f in kept.values() if f.id not in {g.id for g in given}
                      and _meets(f, source, record, since, kinds)]
            found = [f for f in found if f.status != "superseded" or not self._still_given(f)]
        else:
            found = self.handler.facts(source=source, record=record, since=since, kinds=kinds)
            if not latest:
                said = {(f.source, f.record, _said(f.text)) for f in found} | {f.id for f in found}
                found += [f for f in self._kept(self.store.superseded(source=source, record=record))
                          if f.status == "superseded" and _meets(f, source, record, since, kinds)
                          and f.id not in said and (f.source, f.record, _said(f.text)) not in said]
        found.sort(key=lambda f: f.at, reverse=True)
        return found if limit is None else found[:limit]

    def retract(self, fact_id: str) -> None:
        kept = self._kept(self.store.superseded(ids=[fact_id]))
        given = [f for f in self.handler.facts(ids=[fact_id]) if f.status == "current"]
        if not kept and not given:
            self.handler.retract(fact_id)  # raises FactNotFound for an id no layer holds
            return
        if given:
            self.handler.retract(fact_id)
        for fact in kept + given:  # every save of it: the same words in the earlier saves of its record
            same = [row["fact"]["id"] for row in self.store.superseded(source=fact.source, record=fact.record)
                    if _said(str(row["fact"].get("text", ""))) == _said(fact.text)]
            self.store.retract_superseded([fact_id, *same])

    def _still_given(self, fact: Fact) -> bool:
        """Whether the latest save of a superseded fact's record still gives it, by id or in the same words."""
        return any(f.id == fact.id or _said(f.text) == _said(fact.text)
                   for f in self.handler.facts(source=fact.source, record=fact.record))

    def _kept(self, rows: list[dict[str, Any]]) -> list[Fact]:
        """Superseded facts as the store keeps them, masked again on their way out, since a secret may have been
        read after they were kept."""
        from .store import scrubbed

        return [replace(fact_from(scrubbed(row["fact"], self.store.scrub)), status=row["status"],
                        revision=row["revision"], superseded_at=row["at"]) for row in rows]


def superseded(fact: Fact) -> str:
    """How a superseded fact is marked where a model or a person reads it: an earlier save, its revision, its own
    day, and the day of the later save that replaced it."""
    revision = f" (revision {fact.revision.split(':')[-1][:12]})" if fact.revision else ""
    stated = f", stated on {fact.at[:10]}" if fact.at else ""
    return (f"an earlier save{revision}{stated}, superseded on {fact.superseded_at[:10]} by a later save of "
            f"{fact.record}")


def revision_of(record: Record) -> str:
    """A record's revision as its source gives it, or a hash of its body when the source gives none."""
    if record.revision:
        return record.revision
    return "body:" + hashlib.sha256((record.body or "").encode()).hexdigest()[:16]


def saved_revision(store: Any, source: str, record: str) -> str:
    """The revision a record was last taken in at, as `Revisions` keeps it, or "" when it never was."""
    return str((store.state(f"{SAVED}{source}/{record}") or {}).get("revision", ""))


SETTINGS_KEYS = {"facts": {"revisions_kept": None}}


def revisions_kept(settings: dict[str, Any]) -> int:
    """How many earlier saves of one record keep their facts: `facts: revisions_kept:` in `settings.yaml`, which
    `project.check_settings` holds to a whole number, or `REVISIONS_KEPT`."""
    block = settings.get("facts")
    value = block.get("revisions_kept") if isinstance(block, dict) else None
    return REVISIONS_KEPT if value is None else int(value)


def fact_from(data: dict[str, Any]) -> Fact:
    """A fact from the map `asdict` made of it."""
    known = {f.name for f in fields(Fact)}
    values = {k: v for k, v in data.items() if k in known}
    values["quotes"] = [Quote(**q) for q in data.get("quotes") or []]
    values["mentions"] = [Mention(**m) for m in data.get("mentions") or []]
    return Fact(**values)


def _said(text: str) -> str:
    """A fact's words as two saves that say the same thing both give them."""
    return " ".join(text.lower().split())


# -- kinds of fact --------------------------------------------------------------------------------------------------

#: The folder of a workspace that holds its kinds of fact, as Python files of Factoid `Fact` subclasses.
KINDS = "facts"
KIND_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def _snake(name: str) -> str:
    name = name.rsplit(".", 1)[-1]
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def kind_name(klass: type) -> str:
    """The kind a fact of this class carries, the name an agent file filters on: its class name in snake case, an
    underscore before each capital that ends a word, as `OpenQuestion` is `open_question` and `APIKey` is `api_key`."""
    return _snake(klass.__name__)


def load_kinds(root: Path) -> list[type]:
    """The kinds of fact the workspace at `root` defines: each `Fact` subclass a Python file in its `facts/` folder
    defines, file by file in name order. None when it has no such folder. Each file runs on its own, as a module of
    its own, and leaves no compiled copy in the workspace. A file that does not run, a kind with no docstring or a
    guidance that is not text, and two kinds of one name, are each refused with one line that names the file."""
    folder = Path(root) / KINDS
    files = sorted(p for p in folder.glob("*.py") if not p.name.startswith(("_", "."))) if folder.is_dir() else []
    if not files:
        return []
    from factoid import Fact as FactoidFact

    kinds: list[type] = []
    where: dict[str, str] = {}
    for path in files:
        rel = f"{KINDS}/{path.name}"
        module = _run_kinds(path, rel)
        for value in list(vars(module).values()):
            if not (isinstance(value, type) and issubclass(value, FactoidFact)
                    and value.__module__ == module.__name__):
                continue
            name = kind_name(value)
            if not KIND_NAME.fullmatch(name):
                raise KindError(f"{rel}: class {value.__name__} gives the kind {name!r}, which is no plain name: "
                                "name the class in ASCII letters and digits, as OpenQuestion")
            if not (value.__doc__ or "").strip():
                raise KindError(f"{rel}: class {value.__name__} has no docstring, which is what the model reads to "
                                "give a fact this kind: write one line that says what such a fact states")
            if not isinstance(value.guidance, str):
                raise KindError(f"{rel}: class {value.__name__} guidance is not text: write it as a string")
            if name in where:
                raise KindError(f"{rel}: class {value.__name__} gives the kind {name}, as a class in {where[name]} "
                                "does: rename or remove one of them")
            where[name] = rel
            kinds.append(value)
    return kinds


def _run_kinds(path: Path, rel: str) -> types.ModuleType:
    """Run one file of kinds as a module of its own, from its text, so no compiled copy is written beside it."""
    name = "cerebro_kinds_" + hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:16]
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module  # a dataclass in the file looks itself up here
    try:
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 - the workspace's own code, as extensions.py is
    except Exception as error:  # noqa: BLE001 - any error in a file of kinds is reported as one line
        sys.modules.pop(name, None)
        raise KindError(f"{rel} does not run ({type(error).__name__}: {error}): fix it or move it aside") from None
    return module


def kind_notices(wanted: dict[str, list[str]], defined: list[str], held: dict[str, int]) -> list[str]:
    """Lines for the kinds that do not meet. `wanted` is the kinds each agent file filters facts on, by its path,
    `defined` the kinds the facts layer gives, and `held` how many current facts carry each kind.

    A filter on a kind the facts layer does not give reads nothing, which looks like a quiet week, so each such file
    gets a line that lists the kinds there are. A layer that does not say its kinds is judged by the kinds its facts
    carry, and only once it holds a fact. A kind facts carry that the workspace no longer defines gets one line: those
    facts keep it until their record is extracted again."""
    lines = []
    known = defined or sorted(held)
    for path, kinds in wanted.items():
        unknown = [k for k in dict.fromkeys(kinds) if k not in known]
        if not unknown or not known:
            continue
        if defined:
            lines.append(f"{path} filters facts on {', '.join(unknown)}, a kind this workspace does not define (its "
                         f"kinds: {', '.join(defined)}), so that line reads nothing: drop the filter, or define that "
                         f"kind in a file under {KINDS}/")
        else:
            lines.append(f"{path} filters facts on {', '.join(unknown)}, a kind no fact here carries "
                         f"({', '.join(known)}), so that line reads nothing: drop the filter, or give the facts layer "
                         "that kind")
    for kind in sorted(set(held) - set(defined)) if defined else []:
        lines.append(f"{held[kind]} facts carry the kind {kind}, which this workspace does not define now: a fact keeps "
                     f"the kind it was given, and its record takes the kinds under {KINDS}/ when it is extracted again")
    return lines


# -- the documents a record becomes ---------------------------------------------------------------------------------

#: How a Factoid document's uri starts when Cerebro made it: `cerebro:<source>/<record>`, each part quoted.
SCHEME = "cerebro:"
#: The context key that names who on a record is on the workspace's own side, from the record's `ours` field.
OWN_SIDE = "on the workspace's own side"
#: Factoid's own extract prompt, with the record's details after the rules, in a block marked as data.
EXTRACT = Path(__file__).with_name("facts_extract.md.j2")
#: The rules the extractor is told for every fact of every record, before each kind of fact's own guidance. The
#: first is there because Factoid's "grounded" check drops a fact that says more than its passage, so a year, a cause
#: or a judgment the extractor adds loses the whole fact. The second is there because a fact is one claim, and a fact
#: that holds two fails the "atomic" check, and can be lost in its repair.
GUIDANCE = (
    "Rules for every fact from this record:\n"
    "- State only what the passage says. Add no detail it does not give: no year, weekday or time for a date the "
    "passage gives only in part (if it says \"August\" or \"5 August\", write that, not \"August 2025\" or "
    "\"5 August 2025\"), and no cause, consequence, risk or judgment the speaker did not state.\n"
    "- Keep each claim the passage makes, in its own words where you can, each pronoun among them replaced by "
    "what it stands for, and keep who said it. A sentence that "
    "states two claims, such as who someone is and what they did, or an event and what it leads to, gives a fact "
    "for each, and each keeps who said it."
)
#: The rule the extractor is told right after the record's details, outside the block that holds them, since it reads
#: them. It is there because a reader sees each fact alone, so a fact must name what it is about, and a record's
#: details, not its title, which names the record itself, say what that is.
NAMING = ("Name what the record is about in every fact about it, as the details above name it, so the fact reads on "
          "its own; do not leave it implied by \"we\", \"they\", \"our\" or \"the team\". The record's title names "
          "the record itself: leave it out of the facts.")
#: The rule a repair is told, after Factoid's own. It is there because a repair that rewords a fact can fill in what
#: an open reference means, and a guess there gives the fact the wrong subject.
REPAIR_GUIDANCE = (
    'Keep a reference the text leaves open as worded, such as "its" when the text does not say which thing it '
    "means. Never guess what it refers to."
)


def _uri(record: Record) -> str:
    """The uri of the document a record's text becomes, which fixes the document's id."""
    return f"{SCHEME}{quote(record.source, safe='')}/{quote(record.id, safe='')}"


def title(record: Record) -> str:
    """The plain name a person knows a record by, which Factoid shows its models as the document's title: the
    record's `title` field, or else the last part of its id, without a file's extension, with a dash or an underscore
    before a lower case word read as a space, so `local/2026-11-12-launch-sync.md` is `2026-11-12 launch sync`."""
    named = record.fields.get("title")
    if isinstance(named, str) and named.strip():
        return " ".join(named.split())
    stem = PurePosixPath(record.id.rstrip("/") or record.id).stem or record.id
    return re.sub(r"(?<=[a-z\d])[-_]+(?=[a-z])", " ", stem).strip() or stem


def context_of(record: Record) -> dict[str, str]:
    """What the extractor is shown of a record beside its text, its date and its title, from what the record
    carries, each as the record gives it: the collection it is filed under, its source, each header line or key
    under its own word, each other field that holds plain values, who on it is on the workspace's own side, from
    its `ours` field, and who made a record kept whole. A field of nested values is left out, and so is the title,
    which is shown as the title. Nothing here knows what any header names: the extractor reads them."""
    shown: dict[str, str] = {}
    if record.into:
        shown["filed under"] = _line(record.into)
    shown["source"] = _line(record.source)
    for name, value in [*record.keys.items(), *record.fields.items()]:
        word = _line(str(name))
        if not word or word in ("title", "ours") or word in shown or (plain := _plain(value)) is None:
            continue
        shown[word] = plain
    if (ours := _plain(record.fields.get("ours"))) is not None:
        shown[OWN_SIDE] = ours
    if record.by is not None and (by := _line(record.by.name)):
        shown["made by"] = by
    return shown


def _line(text: str) -> str:
    """Text from a record as one line that spells no tag: its runs of white space, line breaks among them, as one
    space, and every `<` escaped, so no name or value can forge a line of the prompt or close the block it is in."""
    return " ".join(text.split()).replace("<", "&lt;")


def _plain(value: Any) -> str | None:
    """A value as one line of text, a list as its items joined; None for one that is empty or holds nested values."""
    if isinstance(value, (list, tuple)):
        items = [_plain(v) for v in value]
        return ("; ".join(i for i in items if i) or None) if all(i is not None for i in items) else None
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, str):
        return _line(value) or None
    return None


@functools.cache
def _record_class(speaker_pattern: str = "") -> type:
    """The Factoid document every record becomes, made once for each `speaker_pattern`, on first use, since Factoid
    is imported only here. `speaker_pattern` is how the lines of a record's source mark who speaks, as its connector
    declares it, empty for a source that declares none: each passage takes the speaker of the last label that starts
    at or before it, read whole from the record's text, and the label stays in the passage's text.

    The extractor is told `GUIDANCE`, then each kind of fact's own guidance, as Factoid joins them, and only then
    shown the record's details, the ones `context_of` built, in one `<record>` block it is told is data from the
    record's source and never instructions, by `EXTRACT`. The details are set on each document and kept nowhere:
    not in its metadata, so a record whose body is the same and whose fields alone changed is not extracted again,
    and not in Factoid's own context, so neither the repair nor the decision model reads them as the document's. The
    class is named as Factoid's own prose document, which Factoid compares when it skips a document it holds, so a
    record Factoid already holds is not extracted again for it. Its checks and its repair are Factoid's own, and
    a repair is told `REPAIR_GUIDANCE` too."""
    from factoid import ProseDocument, template

    label = re.compile(speaker_pattern) if speaker_pattern else None

    class Record(ProseDocument):
        """One record of a workspace's sources, as its facts layer reads it."""

        __qualname__ = "ProseDocument"
        guidance = GUIDANCE
        repair_guidance = REPAIR_GUIDANCE
        extract_prompt = template(str(EXTRACT))
        #: The record's details, as `context_of` built them, set on each document.
        details: ClassVar[dict[str, str]] = {}

        def extract(self, chunk: Any, **context: Any) -> list[Any]:
            # Factoid calls this again with `context` for each half of a chunk whose answer was cut off
            return super().extract(chunk, **{"record": self.details, "naming": NAMING, **context})

        def segment(self) -> list[Any]:
            passages = super().segment()
            speaker = None
            for passage in passages if label else []:
                if found := label.match(self.text, passage.start):  # a sentence split may cut a label in two
                    speaker = " ".join(found.group("speaker").split())
                passage.speaker = speaker
            return passages

    return Record


FIELD_KIND = "field_value"  # the one kind of the facts a record's fields give


def record_name(record: Record) -> str:
    """The words a reader knows a record by: its `name` key, else its `name`, `title` or `subject` field, else the
    plain name `title` makes of it."""
    for value in record.keys.get("name", []):
        if str(value).strip():
            return " ".join(str(value).split())
    for key in ("name", "title", "subject"):
        value = record.fields.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return title(record)


def _as_data(text: str) -> str:
    """Words from a source as plain words in a fact's sentence: on one line, with no citation mark and no heading
    mark, so an agent reads a field's value as data and never as a citation or a part of its article."""
    text = " ".join(text.split()).replace("[^", "[")
    return re.sub(r"(?:^|(?<=\s))#+(?=\s|$)", "", text).strip()


def _value_text(value: Any) -> str:
    """A field's value as words: text as it is, true or false, a number as written, a list's items joined, and any
    other value as JSON. Empty for no value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _as_data(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(t for t in (_value_text(v) for v in value) if t)
    return json.dumps(value, sort_keys=True, default=str)


def field_rows(record: Record) -> list[dict[str, str]]:
    """The rows of a record's fields document: one a field that holds a value, keyed by the field's name, with the
    record's name, the field's name in words, and its value in words. No row holds the record's time, so the same
    fields at a new time are the same document, and cost nothing: a field fact is dated when it was learned. A field
    whose value is the record's name itself is left out: every row names the record by it already, and "The name of
    Alder is Alder." says nothing."""
    name = _as_data(record_name(record))
    rows = []
    for key, value in record.fields.items():
        if (text := _value_text(value)) and text != name:
            rows.append({"key": key, "name": name, "field": _as_data(key.replace("_", " ")) or key,
                         "value": text})
    return rows


FIELD_TEMPLATE = "The {field} of {name} is {value}."


def field_sentence(row: dict[str, str]) -> str:
    """The fact one field gives, in words a reader knows: "The due date of Alder is 2026-11-10."."""
    return FIELD_TEMPLATE.format(**{k: _as_data(str(v)) for k, v in row.items()})


@functools.cache
def _field_document() -> type:
    """The one structured document class a record's fields go to Factoid as, with its one kind of fact, made once
    for the process: Factoid reads a class by its name."""
    from factoid import Fact as FactoidFact
    from factoid import Ignore, StructuredDocument

    field_value = type("FieldValue", (FactoidFact,), {
        "__module__": __name__, "__doc__": "One field a source gives one of its records, and its value."})
    return type("RecordFields", (StructuredDocument,), {
        "__module__": __name__, "__doc__": "The fields a source gives one of its records, one row a field.",
        "columns": {"key": Ignore(), "name": Ignore(), "field": Ignore(), "value": FIELD_TEMPLATE},
        "key": "key", "fact_classes": [field_value]})


# -- Factoid's models, with their keys and hosts --------------------------------------------------------------------

#: The providers Factoid can extract with, and the embedding model its search uses on each.
EMBED = {"fireworks": "fireworks/qwen3-embedding-8b", "openai": "openai/text-embedding-3-small"}
#: What `{call}` holds in a header of each of Factoid's models' calls, by the model's workload.
CALLS = {"generate": "extraction", "embed": "embedding", "choose": "extraction_decision"}
#: Factoid's decision model: Jev on Typesafe, Factoid's default. Factoid can also decide with Fireworks' Rune or
#: OpenAI's decisions API, and keeps them experimental: a workspace needs this one's key to extract.
FACTOID_DECISION = ModelSpec("typesafe", "jev-1.13.0", "", "decision")


def factoid_models(spec: ModelSpec, key: Callable[[str], str] | None = None,
                   gateway: Gateway | None = None) -> dict[str, Any]:
    """Factoid's models for a workspace whose extraction model is `spec`: it becomes Factoid's `generate` model, with
    its effort, and Factoid searches with an embedding model on the same provider. Factoid's default decision model
    decides, `FACTOID_DECISION`, with the floors Factoid sets for it.

    `key` reads a provider's key by its name, as the workspace reads its own models' keys: the secrets store first,
    then the environment. Without it, the key is the provider's own variable in the environment, and nothing else.
    Either way each model reads its key through that reader when Factoid makes a call: Factoid's own provider, which
    loads its key files into the process environment first, is never asked. `gateway` sends each to the host
    settings name for its provider, with their headers."""
    if spec.provider not in EMBED:
        raise FactsSettingsError(f"models: extraction is on {spec.provider}, which Factoid cannot call: set its "
                                 f"provider to {' or '.join(EMBED)}")
    params = {"reasoning_effort": spec.effort} if spec.effort else {}
    read = key or _environment_key
    def make(workload: str, name: str, **p: Any) -> Any:
        route = gateway.route(name.partition("/")[0]) if gateway is not None else None
        return _keyed()(name, read, route, CALLS[workload], **p)

    return {"generate": make("generate", f"{spec.provider}/{spec.model}", **params),
            "embed": make("embed", EMBED[spec.provider]),
            "choose": make("choose", f"{FACTOID_DECISION.provider}/{FACTOID_DECISION.model}")}


def _environment_key(provider: str) -> str:
    """A provider's key from its own variable in the environment, as Cerebro names it, and from nowhere else."""
    known = PROVIDERS.get(provider)
    return os.environ.get(known.variable if known else f"{provider.upper()}_API_KEY", "")


@functools.cache
def _keyed() -> Any:
    """A Factoid model whose provider reads its key through a function. Factoid takes no key but through its
    provider, which reads the environment, and before each call asks `ready`, which first loads Factoid's own key
    files into the environment. So the provider is swapped for one that asks the function and loads no file: Factoid's
    `Model` and `Provider` are its public types, and each call reads `model.provider` afresh. The swap copies every
    field the provider has, whatever Factoid adds to it, and takes the host and headers of the model's route."""
    from factoid import Model
    from factoid.models import Provider

    @dataclass(frozen=True)
    class KeyedProvider(Provider):
        read: Callable[[], str] = field(default=lambda: "", compare=False, repr=False)
        route: Any = field(default=None, compare=False, repr=False)
        call: str = ""

        def api_key(self) -> str | None:
            return self.read().strip() or None

        def base_url(self) -> str:
            own = super().base_url()
            return self.route.url(own) if self.route is not None else own

        def headers(self) -> dict[str, str]:
            return self.route.over(super().headers(), self.call) if self.route is not None else super().headers()

        def ready(self) -> bool:  # Factoid's own loads its key files into the environment first
            return bool(self.api_key() or os.environ.get(self.base_url_env, "").strip())

        def missing_key(self) -> str:
            return (f"no key for {self.name} is kept or set: run cerebro secrets set {self.name}, or export "
                    f"{self.key_env}")

    class KeyedModel(Model):
        def __init__(self, name: str, key: Callable[[str], str], route: Any, call: str, **params: Any):
            super().__init__(name, **params)
            object.__setattr__(self, "_key", key)
            object.__setattr__(self, "_route", (route, call))

        @property
        def provider(self) -> Any:
            base = super().provider
            return KeyedProvider(**{f.name: getattr(base, f.name) for f in fields(base)},
                                 read=lambda: self._key(base.name), route=self._route[0], call=self._route[1])

        def headers(self) -> dict[str, str]:  # Factoid's own builds them without the provider's
            return self.provider.headers()

    return KeyedModel


# -- each model call: its tries, its time and what it spends --------------------------------------------------------

class ModelUnreachable(CerebroError, RuntimeError):
    """A model call of the facts layer failed every try it is given."""


class _Bounded(httpx.BaseTransport):
    """Factoid's HTTP transport, held to `tries` tries in a row, its extraction model's retries and one: past them, it
    raises an error Factoid does not try again, so a pass always ends. Counted per thread. Once a host of `gateway`
    refused a call for its key or for payment due, no call is sent, as Factoid would try another call of the record.
    An error's body is masked whole by `mask` before Factoid cuts it into its error, which no mask could then find."""

    def __init__(self, inner: httpx.BaseTransport, tries: int, gateway: Gateway | None = None,
                 mask: Callable[[str], str] | None = None):
        self.inner, self.tries, self.gateway, self.mask = inner, tries, gateway, mask
        self._local = threading.local()

    def _failed(self, why: str) -> None:
        self._local.tries = tries = getattr(self._local, "tries", 0) + 1
        if tries >= self.tries:
            self._local.tries = 0
            raise ModelUnreachable(f"the facts layer's model did not answer after {tries} tries ({why}): check the "
                                   "network and the provider's status, then the next pass tries again")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.gateway is not None:
            self.gateway.check()
        try:
            response = self.inner.handle_request(request)
        except httpx.TransportError as error:
            self._failed(type(error).__name__)
            raise
        if self.gateway is not None:
            self.gateway.refused_at(str(request.url), response.status_code, "facts layer")
        if response.status_code >= 400 and self.mask is not None:
            said = self.mask(response.read().decode("utf-8", "replace")).encode()
            kept = [(k, v) for k, v in response.headers.items() if k.lower() not in ("content-encoding", "content-length")]
            response = httpx.Response(response.status_code, headers=kept, content=said, request=request)
        if response.status_code in (429, 500, 502, 503, 504, 529):  # what Factoid tries again
            self._failed(f"HTTP {response.status_code}")
            return response
        self._local.tries = 0
        return response

    def close(self) -> None:
        self.inner.close()


_PREPARING: contextvars.ContextVar[_Hooks | None] = contextvars.ContextVar("cerebro_preparing", default=None)


@dataclass(eq=False)
class _Hooks:
    """What each model call asked ahead of a record's turn runs: `hold` before it starts, with what it sends, which
    raises to stop it, and `settle` once it ends, with its model's `provider/model` and the input and output tokens it
    billed."""

    hold: Callable[[dict[str, Any]], Any] | None = None
    settle: Callable[[str, int, int], None] | None = None


def _attributing(meter: Any, lock: threading.RLock) -> Any:
    """Factoid's meter, made to count each call asked ahead of a record's turn apart, in `aside`, so what an
    extraction spent while such calls ran beside it is its own calls alone. `lock` holds while a call is counted and
    while `own` reads, so no read falls between the two counts."""
    from factoid.models import Meter

    class Attributing(Meter):
        aside: dict[str, list[int]]

        def add(self, workload: str, *, input_tokens: int, output_tokens: int, **rest: Any) -> None:
            with lock:
                super().add(workload, input_tokens=input_tokens, output_tokens=output_tokens, **rest)
                if _PREPARING.get() is not None:
                    aside = self.aside.setdefault(workload, [0, 0])
                    aside[0] += input_tokens
                    aside[1] += output_tokens

        def own(self) -> dict[str, tuple[int, int]]:
            """The input and output tokens of each workload the meter counted that no call asked ahead of a turn
            spent."""
            with lock:
                return {workload: (int(row["input_tokens"]) - self.aside.get(workload, [0, 0])[0],
                                   int(row["output_tokens"]) - self.aside.get(workload, [0, 0])[1])
                        for workload, row in self.rows.items()}

    made = Attributing()
    made.rows, made.aside = meter.rows, {}
    return made


def _billed(data: Any) -> tuple[int, int]:
    """The tokens a reply's usage bills, read as Factoid's client reads them."""
    usage = (data.get("usage") if isinstance(data, dict) else None) or {}
    return (int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            int(usage.get("output_tokens") or usage.get("completion_tokens") or 0))


def _metered(app: Any) -> dict[str, tuple[int, int]]:
    """The input and output tokens Factoid's meter counted, by the `provider/model` of the model that spent them,
    less those of calls asked ahead of a record's turn, which run beside an extraction and are credited to their own
    record. A client a test stands in keeps a plain meter."""
    from factoid.models import workload_kind

    meter = app.client.meter
    counted = meter.own() if hasattr(meter, "own") else {
        workload: (int(row["input_tokens"]), int(row["output_tokens"])) for workload, row in meter.rows.items()}
    models, out = app.settings.all_models(), {}
    for workload, (given, taken) in counted.items():
        name = (models.get(workload) or models[workload_kind(workload)]).name
        was = out.get(name, (0, 0))
        out[name] = (was[0] + given, was[1] + taken)
    return out


def _spending(app: Any, work: Callable[[], Any], failed: Callable[[Exception], str]) -> tuple[Any, Extracted]:
    """What `work` gives, and the tokens Factoid's meter counted while it ran. A failure is raised as
    `ExtractionFailed`, in the words `failed` gives it, with what its calls spent before it failed, which is still
    spent."""
    before = _metered(app)

    def spent() -> Extracted:
        models = {name: (given - before.get(name, (0, 0))[0], taken - before.get(name, (0, 0))[1])
                  for name, (given, taken) in _metered(app).items()}
        models = {name: used for name, used in models.items() if any(used)}
        return Extracted([], input_tokens=sum(given for given, _ in models.values()),
                         output_tokens=sum(taken for _, taken in models.values()), models=models)

    try:
        done = work()
    except Exception as error:
        raise ExtractionFailed(failed(error), spent()) from error
    return done, spent()


def _factoids_own(error: Exception) -> bool:
    """Whether `error` is the one Factoid raises when a model call or a date fails, rather than a fault in code."""
    from factoid.dates import DateError
    from factoid.models import ModelError

    return isinstance(error, (ModelError, DateError))


# -- the handler on Factoid -----------------------------------------------------------------------------------------

#: Where Factoid keeps its store, its index and its log of model calls, under the workspace folder.
STATE = Path(".cerebro") / "factoid"
#: Factoid's statuses a reader still sees. Every other one, such as superseded or retracted, reads as `retracted`.
LIVE = ("current", "contradicted")
#: The file beside Factoid's data that says which source and record each document holds, by document id.
ORIGINS = "origins.json"
#: The file beside Factoid's data that counts what Factoid dropped from each document at its last extraction.
DROPPED = "dropped.json"

# Factoid has one active app per process, and its worker threads read the last one made. Every call into Factoid
# holds this lock, with its workspace's app made active first, so two workspaces in one process never mix.
_lock = threading.RLock()


@contextmanager
def _active(app: Any) -> Iterator[Any]:
    """Make `app` Factoid's active app, and hold the lock while the caller works in it. The app moves to the end of
    Factoid's list of apps rather than being added again, so the list does not grow with each call."""
    from factoid import runtime

    with _lock:
        runtime._global[:] = [other for other in runtime._global if other is not app]
        runtime.use(app)
        yield app


class FactoidExtraction(Extraction):
    """The extraction handler on Factoid: one Factoid app per workspace, with its data under its `.cerebro/factoid/`.

    `model` is the workspace's extraction model, the default one if None. `key` reads a provider's key by its name,
    the secrets store first and then the environment, as the workspace reads its own models' keys; without it, the
    provider's own variable in the environment alone, never a key file of Factoid's. `client` stands in for Factoid's
    model client, as in Factoid's own tests, so a test makes no model call. `kinds` is the workspace's kinds of fact,
    as `load_kinds` gives them, read from its `facts/` folder if None. `gateway` sends each model call to the host
    settings name for its provider, with their headers. `mask` masks a model's error reply. `speakers` gives the
    speaker pattern a source's connector declares, by the source's name, and empty for one that declares none."""

    def __init__(self, root: Path, model: ModelSpec | None = None, *, client: Any = None,
                 key: Callable[[str], str] | None = None, kinds: list[type] | None = None,
                 gateway: Gateway | None = None, mask: Callable[[str], str] | None = None,
                 speakers: Callable[[str], str] = lambda source: ""):
        from factoid import Factoid, Settings

        self.root = Path(root)
        self.speakers, self.gateway = speakers, gateway
        self.classes = list(kinds) if kinds is not None else load_kinds(self.root)
        self._kinds = [kind_name(k) for k in self.classes] or ["fact"]
        self.data = self.root / STATE
        self.origins = _read_origins(self.data / ORIGINS)
        model = model or DEFAULTS["extraction"]
        models = factoid_models(model, key, gateway)
        settings = type("WorkspaceSettings", (Settings,), {"models": models, "concurrency": 16})
        make_folder(self.data)
        with _lock, owner_only():  # making an app makes it Factoid's active one, and opens its three SQLite files
            self.app = Factoid(settings, documents=[], entities=[], facts=self.classes, root=self.root,
                               data_dir=self.data)
            if client is not None:
                self.app.client = client
        self._lock_ahead = threading.RLock()
        self._flying: dict[str, threading.Event] = {}  # each request under way, by its key in Factoid's call log
        http = getattr(getattr(self.app, "client", None), "_http", None)
        if client is None and isinstance(http, httpx.Client):  # its own client, which a test has not stood in for
            # Factoid tries a failing call 12 times, for minutes
            tries = model.retries + 1
            http._transport = _Bounded(http._transport, tries, gateway, mask)
            http._mounts = {pattern: _Bounded(mounted, tries, gateway, mask) if mounted is not None else None
                            for pattern, mounted in http._mounts.items()}  # a proxy's
            self.app.client.meter = _attributing(self.app.client.meter, self._lock_ahead)
            self.app.client._post = self._checked(self.app.client._post)

    def _checked(self, post: Callable[..., Any]) -> Callable[..., Any]:
        """Factoid's client's `_post`, through which every model call goes, made to run the hooks of a call asked
        ahead of a record's turn, and to send a request only once no identical one is under way."""

        def checked(workload: str, model: Any, url_path: str, body: dict[str, Any],
                    cache_kind: str | None = None) -> Any:
            from factoid.models import CallLog

            hooks = _PREPARING.get()
            if hooks is not None and hooks.hold is not None:
                hooks.hold(body)  # checked against the cap before it starts
            billed = (0, 0)
            try:
                key = CallLog.key(cache_kind or workload, model.name, body)
                with self._alone(key):  # an identical request under way is waited for, then replayed
                    data, hit = post(workload, model, url_path, body, cache_kind)
                billed = (0, 0) if hit else _billed(data)
                return data, hit
            finally:
                if hooks is not None and hooks.settle is not None:
                    hooks.settle(model.name, *billed)  # what it billed is kept the moment it ends

        return checked

    def kinds(self) -> list[str]:
        return [*self._kinds, FIELD_KIND]

    def needs(self) -> list[ModelSpec]:
        return [FACTOID_DECISION]

    def sent(self, record: Record) -> str:
        """The body, and the record's details once for each part of it Factoid extracts on its own, as each part's
        prompt holds them."""
        body = record.body or ""
        details = "\n".join(f"- {k}: {v}" for k, v in context_of(record).items())
        parts = max(1, -(-len(body) // _record_class().chunk_size))
        return "\n".join([body, *[details] * parts])

    def close(self) -> None:
        """Stop Factoid's worker threads, and drop this app from Factoid's list of apps."""
        from factoid import runtime

        with _lock:
            runtime._global[:] = [other for other in runtime._global if other is not self.app]
            self.app.close()

    # -- taking a record in

    def _document(self, record: Record) -> Any:
        """The Factoid document a record becomes, at its turn and for the calls asked ahead of it alike: its body,
        its time, its details as data, and a uri that fixes its id, so a record sent again replaces the facts it
        gave before."""
        if record.body is None:
            raise NoBody(f"record {record.id} has no body: fetch it whole with the connector's get")
        metadata: dict[str, Any] = {}
        if at := _utc(record.at):
            metadata["source_datetime"] = at
        document = _record_class(self.speakers(record.source))(record.body, metadata=metadata, uri=_uri(record), title=title(record))
        document.details = context_of(record)
        document.sentence_passages = True  # a fact cites the sentences that state it, and keeps them as its quotes
        return document

    def extract(self, record: Record) -> Extracted:
        document = self._document(record)

        def failed(error: Exception) -> str:
            said = f"record {record.id} of {record.source}: factoid failed: {error}"
            if _factoids_own(error):
                # Factoid's error carries its status only in its words, ahead of the reply's: `<model>: HTTP <status>:`
                http = re.match(r"([^/\s]+)/\S+: HTTP (\d{3}):", str(error))
                if http is None:
                    return f"{said}: check the extraction model; the record is tried again next pass"
                via = self.gateway is not None and self.gateway.route(http[1]).base_url
                advice = advice_for(int(http[2]), None if via else "the extraction model and its key")
                return f"{said}: {advice.removesuffix(', so try again later')}; the record is tried again next pass"
            return said

        with _active(self.app) as app:
            self._keep_origin(document.id, record)
            ingested, spent = _spending(app, lambda: app.ingest(document), failed)
            facts = self._load([fact.id for fact in ingested.facts])
            if not ingested.unchanged:  # a document Factoid skipped as unchanged keeps the count it had
                self._keep_dropped(document.id, record, ingested.rejected)
        return replace(spent, facts=facts)

    def extract_fields(self, record: Record) -> Extracted:
        rows = field_rows(record)
        document = _field_document().from_records(rows, uri=f"{_uri(record)}#fields",
                                                  title=f"{record_name(record)}, its fields")

        def failed(error: Exception) -> str:
            why = "" if _factoids_own(error) else ": " + type(error).__name__
            return f"the fields of record {record.id} of {record.source}: factoid failed{why}: {error}"

        with _active(self.app) as app:
            if not rows:  # every field gone from the record: none of its field facts holds now
                self._retire_fields(app, document.id, set())
                return Extracted([])
            self._keep_origin(document.id, record)
            ingested, spent = _spending(app, lambda: app.ingest(document), failed)
            # the same fields again are the document Factoid holds: it gives back what it holds, which is not new
            facts = [] if ingested.unchanged else self._load([fact.id for fact in ingested.facts])
            self._retire_fields(app, document.id, {row["key"] for row in rows})
        return replace(spent, facts=facts)

    @staticmethod
    def _retire_fields(app: Any, document_id: str, kept: set[str]) -> None:
        """Retract each current fact of a fields document whose field the record no longer gives: Factoid leaves a
        row missing from a re-import as it was, and a field gone at the source no longer holds."""
        for fact in app.list_facts(document_id=document_id, status="current"):
            if str((fact.scores.get("cell") or {}).get("key")) not in kept:
                fact.retract("its field is gone from its record")

    # -- calls asked ahead of a record's turn

    @contextmanager
    def _alone(self, key: str) -> Iterator[None]:
        """Send a request once no identical one is under way: a second waits for the first, and Factoid's call log
        then answers it as it answers one asked again."""
        while True:
            with self._lock_ahead:
                under_way = self._flying.get(key)
                if under_way is None:
                    mine = self._flying[key] = threading.Event()
                    break
            under_way.wait()
        try:
            yield
        finally:
            with self._lock_ahead:
                self._flying.pop(key, None)
            mine.set()

    def prepare(self, record: Record, hooks: _Hooks | None = None) -> None:
        """Ask, ahead of a record's turn and beside other records, the model calls of its extraction that read the
        record alone: its passages' embeddings, the extraction of its candidate facts, the kind of each, and their
        embeddings. Nothing is written: each answer lands in Factoid's call log, which the record's `extract` at its
        turn replays, so what it gives is what it gives when asked in turn, and every call that reads what earlier
        records stored, such as checking a fact against the facts stored before it, is asked at the turn, in order.
        Each call runs `hooks`: `hold` before it starts, which may stop it and the calls after it, which are then
        asked at the turn, and `settle` once it ends, with what it billed, which the record's `extract` does not
        count again. It does nothing for a record whose document Factoid holds unchanged, which is not extracted
        again, or a client that keeps no call log."""
        from factoid import runtime
        from factoid.steps import SKIPPED, run_step

        document = self._document(record)
        with _active(self.app) as app:
            if not getattr(app.client, "replay", False) or not hasattr(app.client.meter, "own") or \
                    app.store.has_document(document.id, document.content_hash, type(document).__qualname__,
                                           document.metadata):
                return
        held, preparing = runtime._current.set(app), _PREPARING.set(hooks or _Hooks())  # this app's calls
        try:
            passages = run_step(document, "segment")
            chunks = run_step(document, "chunk", passages)
            step = "situate" if callable(type(document).situate) else "situate_chunk"
            if type(document).situate:
                for chunk in chunks:
                    found = run_step(document, step, chunk)
                    chunk.situation = found if found is not SKIPPED else None
            if chunks:  # as Factoid indexes a document's chunks, before it extracts
                app.client.embed([(f"{c.situation}\n\n" if c.situation else "") + c.text for c in chunks])
            candidates = [fact for chunk in chunks for fact in (run_step(document, "extract", chunk) or [])]
            classes = list(getattr(document, "fact_classes", None) or app.fact_classes)
            kept = []
            for fact in candidates:
                fact = app._classify_fact(fact, classes)
                if fact is not SKIPPED and not run_step(fact, "validate"):
                    kept.append(fact)
            if kept:  # as Factoid embeds the candidates that pass, before it checks them against stored facts
                app.client.embed([fact.text for fact in kept])
        except Exception:  # noqa: BLE001 - whatever stopped it is asked, and said, at the record's turn
            return
        finally:
            _PREPARING.reset(preparing)
            runtime._current.reset(held)

    # -- reading facts back

    def facts(self, *, ids: list[str] | None = None, source: str | None = None, record: str | None = None,
              since: str | None = None, kinds: list[str] | None = None, limit: int | None = None,
              latest: bool = True) -> list[Fact]:
        if ids is not None:
            found = self._load(list(dict.fromkeys(ids)))
        else:
            with _active(self.app) as app:
                live = [f for status in LIVE for f in app.list_facts(status=status)]
                found = [_fact(f, self._origin, self._kind(app, f)) for f in live]
        found = [f for f in found if _meets(f, source, record, since, kinds)]
        found.sort(key=lambda f: f.at, reverse=True)
        return found if limit is None else found[:limit]

    def retract(self, fact_id: str) -> None:
        with _active(self.app) as app:
            fact = app.load_fact(fact_id)
            if fact is None:
                raise FactNotFound(f"there is no fact {fact_id}: query facts for their ids")
            fact.retract("retracted through cerebro")
            left = app.store.query(f"select id from facts where document_id = ? and status in ({', '.join('?' * len(LIVE))})"
                                   " limit 1", (fact.document_id, *LIVE))
            if not left:  # a record with no fact left, as one taken out at its source, counts no drops
                self._keep_dropped(fact.document_id, None, [])

    def search(self, text: str, top_k: int = 10, *, source: str | None = None) -> list[Hit]:
        return self.search_spent(text, top_k, source=source)[0]

    def search_spent(self, text: str, top_k: int = 10, *, source: str | None = None) -> tuple[list[Hit], Extracted]:
        """Factoid embeds the words of each search, and its meter counts the tokens, so what a search spent is what
        the meter gained while it ran. A search Factoid has made before is replayed from its call log, and spends
        none."""
        if not text.strip():
            return [], Extracted([])
        with _active(self.app) as app:
            # With a source, search wider, since hits from other sources are dropped after Factoid ranks them.
            pool = top_k if source is None else max(top_k, app.settings.search_candidates)
            hits, spent = _spending(app, lambda: app.search_facts(text, top_k=pool, contradictions=False),
                                    lambda error: f"a facts search failed: {error}")
            origins = {id: self._origin(id) for id in {hit.fact.document_id for hit in hits}}
        out = []
        for hit in hits:
            src, rec = origins[hit.fact.document_id]
            if source is None or src == source:
                out.append(Hit(hit.fact.id, hit.score, {"source": src, "record": rec, "validity": hit.validity}))
        return out[:top_k], spent

    def _load(self, ids: list[str]) -> list[Fact]:
        """The facts with these ids, whatever their status, in that order. An id Factoid does not hold is left out."""
        with _active(self.app) as app:
            loaded = [f for f in (app.load_fact(id) for id in ids) if f is not None]
            return [_fact(f, self._origin, self._kind(app, f)) for f in loaded]

    def _kind(self, app: Any, fact: Any) -> str:
        """A fact's kind, from its class. A field's fact, or one of a kind the workspace no longer defines, comes back as
        Factoid's own class, so its kind is read from the class name Factoid kept for it: it keeps the kind it was
        given."""
        klass = type(fact)
        if klass.__module__.split(".")[0] == "factoid":
            row = app.store.fact_row(fact.id)
            return _snake(row["class"]) if row is not None else kind_name(klass)
        return kind_name(klass)

    def _origin(self, document_id: str) -> tuple[str, str]:
        """The source and record of a Factoid document, from `origins.json`, read again for one another process noted
        since, or empty for one Cerebro did not make."""
        if document_id not in self.origins:
            self.origins = _read_origins(self.data / ORIGINS)
        source, record = self.origins.get(document_id, ("", ""))
        return source, record

    # -- the files beside Factoid's data

    def _keep_origin(self, document_id: str, record: Record) -> None:
        """Note which source and record a document holds, in `origins.json`, before Factoid reads it. The file is
        read and written under its lock, so a note another process made since stays."""
        if self.origins.get(document_id) == [record.source, record.id]:
            return
        make_folder(self.data)
        path = self.data / ORIGINS
        with _file_lock(path):
            self.origins = {**_read_origins(path), document_id: [record.source, record.id]}
            _write_whole(path, self.origins)

    def dropped(self) -> dict[str, dict[str, Any]]:
        """What Factoid's checks dropped from each record at its last extraction, from `dropped.json` as it is now."""
        return dict(sorted((row["record"], {"candidates": row["candidates"], "why": row["why"]})
                           for row in _read_dropped(self.data / DROPPED).values()))

    def _keep_dropped(self, document_id: str, record: Record | None, rejected: list[Any]) -> None:
        """Count the candidates Factoid dropped from a document, by the checks they failed, in `dropped.json`: a
        check by its own name, as `grounded`, and a code check's line as Factoid writes it. A document that lost
        none leaves the file. The file is read and written under its lock, as the trust file is, so two processes
        that extract at once each keep their own row."""
        why: dict[str, int] = {}
        for candidate in rejected:
            for reason in candidate.reasons or ["unknown"]:
                name = reason if reason.startswith("validate:") else reason.rsplit(".", 1)[-1]
                why[name] = why.get(name, 0) + 1
        row = {"record": f"{record.source}/{record.id}", "candidates": len(rejected),
               "why": dict(sorted(why.items()))} if rejected and record is not None else None
        make_folder(self.data)
        path = self.data / DROPPED
        with _file_lock(path):
            kept = _read_dropped(path)
            if kept.get(document_id) == row:
                return
            if row is None:
                kept.pop(document_id, None)
            else:
                kept[document_id] = row
            _write_whole(path, kept)


def _fact(fact: Any, origin: Callable[[str], tuple[str, str]], kind: str) -> Fact:
    """A Factoid fact as Cerebro's `Fact`, of the kind given. `origin` gives the source and record of a document: the
    fact's own, and each quote's. Its time is its record's, or when the record gives none, when it was extracted. A
    quote has no side, since Factoid does not give one."""
    mentions = [Mention(m.text, m.role or "about") for m in fact.mentions]
    subject = next((m.text for m in mentions if m.role == "subject"), mentions[0].text if mentions else "")
    source, record = origin(fact.document_id)
    return Fact(id=fact.id, text=fact.text, subject=subject, kind=kind,
                at=_utc(fact.source_datetime or fact.extracted_at), source=source, record=record,
                quotes=[Quote(q.text, q.speaker or "", _utc(q.said_at), passage=q.passage_n,
                              record=origin(q.document_id)[1]) for q in fact.quotes],
                mentions=mentions, status="current" if fact.status in LIVE else "retracted",
                field=str(((getattr(fact, "scores", None) or {}).get("cell") or {}).get("key") or "")
                if kind == FIELD_KIND else "")


def _utc(value: datetime | str | None) -> str:
    """A time as `2026-11-01T09:00:00Z`. A time with no zone is taken as UTC. Empty if there is none, or it is not a
    time."""
    if not value:
        return ""
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(value.strip())
    except ValueError:
        return ""
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_origins(path: Path) -> dict[str, list[str]]:
    """Each document's source and record, as `origins.json` holds them now."""
    return {k: v for k, v in _read_json(path).items() if isinstance(v, list) and len(v) == 2}


def _read_json(path: Path) -> dict[str, Any]:
    """The JSON object in a file beside Factoid's data, or an empty one when the file is missing, does not read, or
    holds something else."""
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return kept if isinstance(kept, dict) else {}


def _read_dropped(path: Path) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in _read_json(path).items() if isinstance(v, dict) and isinstance(v.get("record"), str)
            and isinstance(v.get("candidates"), int) and isinstance(v.get("why"), dict)}


def _write_whole(path: Path, data: dict[str, Any]) -> None:
    """Write `data` to `path` as JSON in one step, so a reader finds the old file or the new one, never part of one."""
    part = path.with_suffix(".part")
    write_file(part, json.dumps(data, sort_keys=True))
    os.replace(part, path)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """A lock beside `path`, held across one read and its write, so a change another process makes at the same time
    waits and then reads what this one wrote. Where the system has no such lock, a change is made without it."""
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - a system with no flock
            fcntl = None  # type: ignore[assignment]
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
