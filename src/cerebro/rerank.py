"""Re-ranking: the decision model reorders the best hits of a `matching` search, and it is off unless settings say.

    search:
      rerank: true    # off when left out
      rerank_top: 20  # how many of the best hits it scores, from 1 to 50; 20 when left out

`DecisionReranker` is the re-rank function `search.reorder` calls. It scores each of the best `top` hits against the
words searched, the way the `keep` judgment scores a passage: one decision for them all, a yes or no for each, its
probability of yes the hit's score. The scored hits come first, highest score first, a tie in their first order, and
every other hit keeps its first order after them. A hit comes back as it was handed in: its score is never the
model's, and no score reaches a caller. The words of the judgment are the bundled prompt `rerank-judge`, or the
workspace's own copy at `prompts/rerank-judge.md`.

A search never waits long and never fails for it. Each call is held to the `rerank` layer's daily cap: it is checked
against the cap before it starts, and is recorded after it, as the keep judge's calls are held to compilation. The
calls of one search run in a thread of their own, and the search waits at most `limit` seconds, by the `ticks`
counter, for them all. Past the cap, past the time bound, and on any failure, the hits come back in the order they
were handed in, with a note that says why, and `order` gives that note for the reply. A call the search stopped
waiting for still finishes, and its spend is recorded and its scores are kept.

A score is cached by the decision provider and model, the judgment's words, the words searched, and the digest of
what the model read of the hit, so a search asked again makes no call. The cache is held in memory and keeps at most
`RERANK_CACHE_ENTRIES` scores, 4,096; past that the oldest, by when it was last kept or read, is dropped first.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import prompts
from .article import passages
from .budget import BudgetSpent, Ledger
from .clock import Clock, seconds, system_clock
from .errors import CerebroError
from .models import Decider, DecisionTooLarge, ModelSpec, TokenCount, YesNo
from .search import ARTICLE, FACT, RERANK_TOP, Hit

if TYPE_CHECKING:
    from .facts import Extraction
    from .store import Store

__all__ = ["LAYER", "RERANK_CACHE_ENTRIES", "RERANK_CALL_SECONDS", "RERANK_PROMPT", "RERANK_SECONDS", "RERANK_TOP_DEFAULT",
           "SETTINGS_KEYS", "DecisionReranker", "Reordered", "RerankSettings", "RerankSettingsError", "read_rerank"]

LAYER = "rerank"  # the budget layer its calls count in
RERANK_PROMPT = "rerank-judge"  # the bundled prompt the judgment reads, which a workspace may eject
RERANK_TOP_DEFAULT = 20  # how many of the best hits it scores when settings name no `rerank_top`
RERANK_SECONDS = 5.0  # the longest a search waits for its re-ranking
# The longest the call a search stopped waiting for may go on: the decision client gives up its connection after this
# long with no byte, and asks once, so no call holds a thread for the life of a served workspace.
RERANK_CALL_SECONDS = 20.0
RERANK_CACHE_ENTRIES = 4096  # the most scores the cache keeps, the oldest dropped first
RERANK_PASSAGE_BYTES = 800  # what of one hit the model reads
RERANK_QUESTION = "Does passage {id} answer the search?"
SKIPPED = "the best matches are in search order, not re-ranked"


class RerankSettingsError(CerebroError, ValueError):
    """A `search:` setting Cerebro cannot run on."""


@dataclass(frozen=True)
class RerankSettings:
    """Whether `matching` re-ranks its best hits, and how many it scores."""

    on: bool = False
    top: int = RERANK_TOP_DEFAULT


SETTINGS_KEYS = {"search": {"rerank": None, "rerank_top": None}}


def read_rerank(settings: Mapping[str, Any]) -> RerankSettings:
    """The `search:` block of settings. An absent block, or one with no `rerank`, leaves re-ranking off."""
    block = settings.get("search")
    if block is None:
        return RerankSettings()
    if not isinstance(block, dict):
        raise RerankSettingsError("settings.yaml search is not a map: write rerank: true and rerank_top: 20 under it, "
                                  "or leave it out")
    on = block.get("rerank", False)
    if on is None:
        on = False
    if not isinstance(on, bool):
        raise RerankSettingsError(f"settings.yaml search.rerank is {on!r}: write true or false")
    top = block.get("rerank_top", RERANK_TOP_DEFAULT)
    if top is None:
        top = RERANK_TOP_DEFAULT
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= RERANK_TOP:
        raise RerankSettingsError(f"settings.yaml search.rerank_top is {top!r}: write a whole number from 1 to "
                                  f"{RERANK_TOP}, the most hits a search hands its re-ranking")
    return RerankSettings(on, top)


@dataclass
class Reordered:
    """What a re-ranking gave: the hits in their new order, or in the order handed in with `note` saying why."""

    hits: list[Hit]
    note: str = ""


@dataclass
class _Passage:
    """What the model reads of one hit."""

    kind: str
    title: str
    heading: str
    text: str

    def digest(self) -> str:
        return hashlib.sha256(f"{self.kind}\0{self.title}\0{self.heading}\0{self.text}".encode()).hexdigest()[:24]

    def wire(self, id: str, mask: Callable[[str], str]) -> dict[str, str]:
        """What the model reads of it, masked, then cut: a cut first could leave part of a secret no mask finds."""
        text = mask(self.text).encode()[:RERANK_PASSAGE_BYTES].decode("utf-8", "ignore")
        if self.kind == FACT:
            return {"id": id, "kind": FACT, "text": text}
        return {"id": id, "kind": "article part", "article": self.title, "heading": self.heading, "text": text}


@dataclass
class _Asked:
    """One search's calls, shared with the thread that makes them."""

    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    scores: dict[str, float] = field(default_factory=dict)
    why: str = ""


class DecisionReranker:
    """The re-rank function on the decision model, as the module's docstring gives it. `store` and `extraction` are
    where it reads the text of a hit: the text under an article's heading from the store, a fact from the facts layer.
    With a `ledger`, each call is held to the `rerank` cap and recorded, priced on `decision`. `clock` gives the time the
    ledger reads, and `ticks` the seconds the time bound reads. `mask` masks the words searched and every passage
    before the model reads them. `masks`, in its place, gives a mask of every value the secrets store holds, built
    for each call. `calls` and `usage` total what it spent."""

    def __init__(self, decider: Decider, *, store: Store | None = None, extraction: Extraction | None = None,
                 ledger: Ledger | None = None, decision: ModelSpec | None = None, clock: Clock = system_clock,
                 ticks: Callable[[], float] = seconds, root: Path | None = None, top: int = RERANK_TOP_DEFAULT,
                 limit: float = RERANK_SECONDS, entries: int = RERANK_CACHE_ENTRIES,
                 mask: Callable[[str], str] | None = None, masks: Callable[[], Callable[[str], str]] | None = None):
        self.decider, self.store, self.extraction = decider, store, extraction
        self.mask = mask or (lambda text: text)
        self.masks = masks
        self.ledger, self.decision, self.clock, self.ticks = ledger, decision, clock, ticks
        self.root, self.top, self.limit, self.entries = root, top, limit, entries
        self.calls, self.usage = 0, TokenCount()
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def __call__(self, text: str, hits: list[Hit]) -> list[Hit]:
        return self.order(text, hits).hits

    def order(self, text: str, hits: list[Hit]) -> Reordered:
        """`hits` with the best `top` reordered by the model's scores, or as handed in with a note. Never raises."""
        top = hits[:self.top]
        if not top:
            return Reordered(list(hits))
        started = self.ticks()
        try:
            contract = prompts.load(RERANK_PROMPT, self.root)
            passages = self._passages(top)
        except Exception as error:  # noqa: BLE001 - a search never fails for its re-ranking
            return self._skipped(hits, f"its hits could not be read: {error}")
        keys = [self._key(contract, text, p) for p in passages]
        scores: dict[str, float] = {}
        with self._lock:
            for key in keys:
                if key in self._cache:
                    scores[key] = self._cache[key]
                    self._cache.move_to_end(key)
        missing = list(dict.fromkeys(k for k in keys if k not in scores))
        if missing:
            wanted = {key: passages[keys.index(key)] for key in missing}
            asked = _Asked()
            threading.Thread(target=self._work, args=(contract, text, wanted, asked), daemon=True,
                             name="cerebro-rerank").start()
            finished = asked.done.wait(max(0.0, self.limit - (self.ticks() - started)))
            if not finished or self.ticks() - started > self.limit:
                asked.stop.set()
                return self._skipped(hits, f"the decision model did not answer within {self.limit:g} seconds, the "
                                           "most a search waits for it: search again later")
            if asked.why:
                return self._skipped(hits, asked.why)
            scores.update(asked.scores)
        order = sorted(range(len(top)), key=lambda i: -scores[keys[i]])
        return Reordered([top[i] for i in order] + hits[len(top):])

    def _skipped(self, hits: list[Hit], why: str) -> Reordered:
        return Reordered(list(hits), f"{SKIPPED}: {why}")

    def _key(self, contract: str, text: str, passage: _Passage) -> str:
        model = f"{self.decision.provider}\0{self.decision.model}" if self.decision is not None else "\0"
        whole = f"{model}\0{contract}\0{text.strip()}\0{passage.digest()}"
        return hashlib.sha256(whole.encode()).hexdigest()[:32]

    def _keep(self, key: str, score: float) -> None:
        with self._lock:
            self._cache[key] = score
            self._cache.move_to_end(key)
            while len(self._cache) > self.entries:
                self._cache.popitem(last=False)

    def _work(self, contract: str, text: str, wanted: dict[str, _Passage], asked: _Asked) -> None:
        """The calls for the hits the cache lacks, in this search's own thread. Whatever fails is kept as `why`."""
        try:
            self._ask(contract, text, list(wanted.items()), asked)
        except BudgetSpent as error:
            asked.why = str(error)
        except Exception as error:  # noqa: BLE001 - a search never fails for its re-ranking
            asked.why = f"the decision model failed: {error}"
        finally:
            asked.done.set()

    def _ask(self, contract: str, text: str, batch: list[tuple[str, _Passage]], asked: _Asked) -> None:
        """One decision for `batch`, split in halves while it is too large. Each call is checked against the cap
        before it starts, and recorded after it."""
        if asked.stop.is_set() or asked.why:
            return
        ids, mask = [f"p{n}" for n in range(1, len(batch) + 1)], self.masks() if self.masks else self.mask
        state = {"contract": contract, "search": mask(text.strip()),
                 "passages": [{k: mask(v) for k, v in p.wire(id, mask).items()} for id, (_, p) in zip(ids, batch)]}
        questions = {id: YesNo(RERANK_QUESTION.format(id=id)) for id in ids}
        if self.ledger is not None:
            self.ledger.check(LAYER, self.clock(), self.decision)
        try:
            decision = self.decider.decide(state, questions)
            with self._lock:
                u = decision.usage
                self.calls += 1
                self.usage = TokenCount(self.usage.input_tokens + u.input_tokens,
                                        self.usage.output_tokens + u.output_tokens,
                                        self.usage.cache_read_tokens + u.cache_read_tokens,
                                        self.usage.cache_write_tokens + u.cache_write_tokens)
            if self.ledger is not None and self.decision is not None:
                self.ledger.record(LAYER, self.clock(), self.decision, decision.usage, "rerank")
        except DecisionTooLarge:
            if len(batch) < 2:
                raise
            half = len(batch) // 2
            self._ask(contract, text, batch[:half], asked)
            self._ask(contract, text, batch[half:], asked)
            return
        for id, (key, _) in zip(ids, batch):
            answer = decision.answers.get(id)
            yes = answer.yes if answer is not None else None
            if yes is None or not 0 <= yes <= 1:
                asked.why = f"the decision model left passage {id} of the search unscored"
                continue
            self._keep(key, float(yes))
            asked.scores[key] = float(yes)

    def _passages(self, hits: list[Hit]) -> list[_Passage]:
        """What the model reads of each hit: the text under an article's heading, from the store, or a fact's text."""
        facts: dict[str, Any] = {}
        unread = [hit.id for hit in hits if _is_fact(hit) and not hit.attributes.get("text")]
        if unread and self.extraction is not None:
            facts = {f.id: f for f in self.extraction.facts(ids=unread)}
        out: list[_Passage] = []
        for hit in hits:
            if _is_fact(hit):
                fact = facts.get(hit.id)
                said = str(hit.attributes.get("text") or (fact.text if fact is not None else ""))
                out.append(_Passage(FACT, "", "", said))
                continue
            article_id = str(hit.attributes.get("article", hit.id))
            article = self.store.article(article_id) if self.store is not None else None
            title = article.identity.title if article is not None else str(hit.attributes.get("title", ""))
            compilations = passages(article) if article is not None else []
            if "article" in hit.attributes:  # a compilation's row of the index, `<article>#<place>`
                place = hit.id.rpartition("#")[2]
                at = int(place) if place.isdigit() else -1
                compilation = compilations[at] if 0 <= at < len(compilations) else None
            else:  # an article of a search across everything, under the heading that matched best
                heading, spot = str(hit.attributes.get("heading", "")), int(hit.attributes.get("place", -1))
                compilation = compilations[spot] if 0 <= spot < len(compilations) else next(
                    (s for s in compilations if s.heading == heading), compilations[0] if compilations else None)
            out.append(_Passage(ARTICLE, title or article_id, compilation.heading if compilation else "",
                                compilation.text if compilation else ""))
        return out


def _is_fact(hit: Hit) -> bool:
    """A fact's hit: one the facts layer's search gave, or a fact of a search across everything."""
    kind = hit.attributes.get("kind")
    return kind == FACT or (kind is None and "article" not in hit.attributes)
