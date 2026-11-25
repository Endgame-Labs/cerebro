"""Search indexes, one namespace per kind of text, and the index that follows the store by itself.

Articles sit in their own namespace, apart from Factoid's index of facts. An index never reads the store: what it
searches is what was added to it. The seam starts from Factoid's `Index`, cut to one ranked search, since ranking
here is BM25 re-ranked and the re-ranking belongs to the caller. A filter on an attribute that holds a list matches
when any item equals the value.

An article is indexed as chunks: one row per passage, each heading of its text with what is under it, as
`article.passages` cuts it, its text under its title and heading, with the article's id, collection, title and the
passage's heading as attributes, empty for text before any heading. An article with nothing written yet is one row of
its title.
`sync` keeps the index up to the store: it reads what the store says changed since the cursor it last reached, and
re-indexes each article written since. Whoever searches calls it first, so nothing that writes an article has to
call the index. An index that keeps its rows on disk keeps that cursor beside them, as a `DurableIndex`. For any
other, the cursor lives as long as the index object does.

`ranked` searches, then hands the best hits to a re-rank function, if one is given, to reorder them. No model is
called here.

A search across everything asks both namespaces, the wiki's articles and the facts layer's facts, and `fuse` merges
the two rankings into one list. The two engines' scores do not compare, so the merge goes by rank and never by score:
reciprocal rank fusion, as `fuse` states. A fact linked to an article in the list is folded under it, so the list
does not hold the same thing twice. Each item of the merged list is a `Found`: what it is, where it came from, and the
id `read` takes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from .article import passages
from .errors import CerebroError

if TYPE_CHECKING:
    from .article import Article
    from .facts import Fact
    from .store import Store

__all__ = ["ARTICLE", "ARTICLES", "EVERYTHING", "FACT", "FUSE_K", "RERANK_TOP", "TOP", "DurableIndex", "Found", "Hit",
           "Index", "Rerank", "RerankError", "chunks", "fuse", "ranked", "reorder", "sync"]

ARTICLES = "articles"  # the namespace articles are indexed in
RERANK_TOP = 50  # how many of the best hits a re-rank function sees and may reorder
TOP = 10_000  # the most hits a search for a query's `matching` ranks
FUSE_K = 60  # reciprocal rank fusion's constant: the larger, the less the very first places outweigh the rest
EVERYTHING = "articles and facts"  # what a search across everything searched, as its reply names it
ARTICLE, FACT = "article", "fact"  # the two kinds of item a search across everything gives


@dataclass
class Hit:
    """One search result: the row's id, its score (higher is better), and its attributes."""

    id: str
    score: float
    attributes: dict[str, Any] = field(default_factory=dict)


class Index:
    """The seam every search backend implements."""

    def upsert(self, namespace: str, rows: list[dict[str, Any]]) -> None:
        """Add or replace rows. Each row holds `id` and `text`; every other key is a filterable attribute."""
        raise NotImplementedError

    def delete(self, namespace: str, ids: list[str]) -> None:
        raise NotImplementedError

    def search(self, namespace: str, text: str, top_k: int, filters: dict[str, Any] | None = None) -> list[Hit]:
        """The rows whose text matches, best first, at most `top_k`, among rows whose attributes match `filters`."""
        raise NotImplementedError


class DurableIndex(Index):
    """An index that outlives the process, and keeps the cursor `sync` reached beside its rows."""

    def ids(self, namespace: str, filters: dict[str, Any]) -> list[str]:
        """The ids of the rows whose attributes match `filters`, whatever their text."""
        raise NotImplementedError

    def synced(self, namespace: str) -> int:
        """The store cursor `sync` last reached for this namespace, or 0."""
        raise NotImplementedError

    def mark(self, namespace: str, cursor: int) -> None:
        """Keep the cursor `sync` reached. It never moves back."""
        raise NotImplementedError


@dataclass
class Found:
    """One item of a search across everything: an article or a fact, and enough to `read` it next.

    `kind` is `article` or `fact`, and `id` is what `read` takes. `source` is where it came from: an article's
    collection, or the source a fact's record came from. `title` is an article's title or a fact's text, and `item`
    is the article or the fact whole. An article's `heading` is the heading of the passage that matched best, and
    its `folded` are the facts the search found that are linked to it, best first, each whole, so each can be read by
    its own id, and its `place` is where that passage sits among `article.passages`, or -1. A fact's `folded` is
    always empty.
    """

    kind: str
    id: str
    source: str
    title: str
    item: Article | Fact
    folded: list[Fact] = field(default_factory=list)
    heading: str = ""
    place: int = -1


# A re-rank function takes the words searched for and the best hits, best first, and gives them back reordered.
Rerank = Callable[[str, list[Hit]], list[Hit]]


def chunks(article: Article) -> list[dict[str, Any]]:
    """An article's rows: one per passage, by its place, or one of its title when it has none."""
    who = article.identity
    base = {"article": who.id, "collection": who.collection, "title": who.title}
    found = passages(article)
    if not found:
        return [{"id": f"{who.id}#0", "text": who.title, **base, "heading": ""}]
    return [{"id": f"{who.id}#{n}", "text": f"{who.title}\n{s.heading}\n{s.text}", **base, "heading": s.heading}
            for n, s in enumerate(found)]


@dataclass
class _Kept:
    """What `sync` keeps for an index that does not keep it itself: the cursor, and each article's row ids."""

    cursor: int = 0
    rows: dict[str, list[str]] = field(default_factory=dict)


_kept: WeakKeyDictionary[Index, dict[str, _Kept]] = WeakKeyDictionary()
_syncing = threading.Lock()


def sync(index: Index, store: Store) -> int:
    """Bring `index` up to `store`: re-index each article written since the cursor last reached, and drop the rows
    of a heading it no longer has. Gives the cursor it reached. It reads and writes inside one exclusive block of the
    store, so a sync in another process cannot write newer text that this one then writes older text over."""
    with _syncing, store.exclusive():
        kept = _Kept(index.synced(ARTICLES)) if isinstance(index, DurableIndex) \
            else _kept.setdefault(index, {}).setdefault(ARTICLES, _Kept())
        changed, later = store.changed_since(kept.cursor)
        for kind, id in changed:
            if kind != "article":
                continue
            article = store.article(id)
            rows = chunks(article) if article is not None else []
            now = [row["id"] for row in rows]
            if isinstance(index, DurableIndex):  # its filter ignores case, and an id does not
                before = [row for row in index.ids(ARTICLES, {"article": id}) if row.rpartition("#")[0] == id]
            else:
                before = kept.rows.get(id, [])
            if stale := [row for row in before if row not in now]:
                index.delete(ARTICLES, stale)
            if rows:
                index.upsert(ARTICLES, rows)
            kept.rows[id] = now
        if later != kept.cursor:
            if isinstance(index, DurableIndex):
                index.mark(ARTICLES, later)
            kept.cursor = later
        return later


def ranked(index: Index, namespace: str, text: str, top_k: int, filters: dict[str, Any] | None = None,
           rerank: Rerank | None = None) -> list[Hit]:
    """The rows that match `text`, best first, with the best `RERANK_TOP` reordered by `rerank` if one is given."""
    return reorder(text, index.search(namespace, text, top_k, filters), rerank)


def reorder(text: str, hits: list[Hit], rerank: Rerank | None = None) -> list[Hit]:
    """`hits` with the best `RERANK_TOP` reordered by `rerank`, or as they are when there is none."""
    if rerank is None:
        return hits
    top = hits[:RERANK_TOP]
    again = rerank(text, list(top))
    if sorted(h.id for h in again) != sorted(h.id for h in top):
        raise RerankError(f"the re-rank function gave back {len(again)} hits for the {len(top)} it was given, or "
                          "other ids: reorder the hits it is handed, and add or drop none")
    return again + hits[RERANK_TOP:]


def fuse(articles: list[Hit], facts: list[Hit], links: Mapping[str, list[str]]) -> list[Hit]:
    """One list from a ranking of articles and a ranking of facts, each best first, merged by rank and never by score.

    The rule is reciprocal rank fusion. An item at place r of a ranking, counted from 1, earns 1 / (FUSE_K + r) from
    it, and its place in the merged list goes by the sum of what it earns, highest first. Scores are never read, since
    the two engines' scores do not compare. A fact whose `links` name an article in the list is folded under it, the
    best placed if it names more than one, and is no item of its own: its article is then in the facts' ranking too,
    at the place of the best fact folded under it, and earns from both. Every other fact is an item at its own place.
    A tie goes to the item at the better place in its own ranking, and between an article and a fact at the same
    place, to the article. A ranking that names an id twice counts its first place.

    Each hit given back is the item's own, with its id, the sum as its score, and its attributes, plus `kind`,
    `article` or `fact`, and `folded`, the ids of the facts folded under an article, best first.
    """
    place: dict[str, int] = {}
    first: dict[str, Hit] = {}
    for hit in articles:
        if hit.id not in place:
            place[hit.id], first[hit.id] = len(place), hit
    folded: dict[str, list[str]] = {id: [] for id in place}
    in_facts: dict[str, int] = {}  # an article's place in the facts' ranking: that of its best folded fact
    alone: dict[str, tuple[int, Hit]] = {}
    seen: set[str] = set()
    for n, hit in enumerate(facts):
        if hit.id in seen:
            continue
        seen.add(hit.id)
        homes = [id for id in links.get(hit.id, []) if id in place]
        if not homes:
            alone[hit.id] = (n, hit)
            continue
        home = min(homes, key=place.__getitem__)
        folded[home].append(hit.id)
        in_facts.setdefault(home, n)
    entries: list[tuple[float, int, Hit]] = []  # articles first, so the stable sort puts one before a tied fact
    for id, n in place.items():
        score = 1 / (FUSE_K + n + 1) + (1 / (FUSE_K + in_facts[id] + 1) if id in in_facts else 0)
        entries.append((score, n, Hit(id, score, first[id].attributes | {"kind": ARTICLE, "folded": folded[id]})))
    for id, (n, hit) in alone.items():
        score = 1 / (FUSE_K + n + 1)
        entries.append((score, n, Hit(id, score, hit.attributes | {"kind": FACT, "folded": []})))
    entries.sort(key=lambda entry: (-entry[0], entry[1]))
    return [entry[2] for entry in entries]


class RerankError(CerebroError, ValueError):
    """A re-rank function gave back other hits than it was handed."""
