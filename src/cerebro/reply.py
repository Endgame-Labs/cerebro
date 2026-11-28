"""What an operation gives a caller outside Cerebro, as data: one shape for MCP's structured content, the JSON API,
`Cerebro.call` and `cerebro tools --json`.

Every empty field is left out, at any depth: an empty string, list or map, and a null. A number or a yes or no stays,
since `count: 0` or `withdrawn: false` says something. An article is its id, its title, its keys, when it was last
written, its status when it is not active, its properties, and its whole text as one markdown string, citation markers
in place, so a caller reads a fact by the id the text names. A read adds the facts Cerebro learned since the article was
last written, and never the rest of its facts. A fact is its id, its text, its date, its record and the words of the
source that hold it: when another record holds some, each record's words in their own quotation marks, another's after
its name. A fact read by the articles it is linked to and not about them says so: `relation: mentions` when it names
them, and `via: record` when it reaches them only through a record of theirs. A search hit on an article is its id and
the passage that matched.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .article import Article, body, compiled_at, fields, passages, pending, written
from .connector import Record
from .facts import Fact
from .ground import quote_named
from .lookup import QueryResult, ReadResult
from .records import DecideResult
from .search import ARTICLE, Found
from .store import MENTIONS, RECORD

__all__ = ["SINCE_WRITTEN", "article_text", "outside", "pruned"]

SINCE_WRITTEN = 20  # the most facts learned since an article was written that a read gives with it
PLAIN_KIND = "fact"


def pruned(value: Any) -> Any:
    """`value` with every empty string, list or map, and every null, left out of each map in it, at any depth."""
    if isinstance(value, dict):
        kept = {k: pruned(v) for k, v in value.items()}
        return {k: v for k, v in kept.items() if v is not None and v != "" and v != [] and v != {}}
    if isinstance(value, list):
        return [pruned(v) for v in value]
    return value


def article_text(article: Article) -> str:
    """The article's text as the wiki holds it: each part in order, as markdown."""
    return body(article)


def _fact(fact: Fact, stale: dict[str, str], quote: bool = True, ties: dict[str, str] | None = None) -> dict[str, Any]:
    tie = (ties or {}).get(fact.id)
    return {"id": fact.id, "text": fact.text, "kind": fact.kind if fact.kind != PLAIN_KIND else None,
            "relation": tie if tie == MENTIONS else None, "via": tie if tie == RECORD else None,
            "at": fact.at, "record": fact.record, "quote": quote_named(fact) if quote else None,
            "status": fact.status if fact.status != "current" else None, "out_of_date": stale.get(fact.id),
            **({"revision": fact.revision, "superseded_at": fact.superseded_at} if fact.status == "superseded" else {})}


def _article(article: Article, result: ReadResult | None = None) -> dict[str, Any]:
    who = article.identity
    out: dict[str, Any] = {"id": who.id, "title": who.title, "status": who.status if who.status != "active" else None,
                           "keys": who.keys or None, "compiled_at": compiled_at(article) or None,
                           "pending": True if pending(article) else None,  # shown, never passed off as written
                           "properties": {name: prop.value for name, prop in fields(article).items()}}
    if result is not None:
        out |= {"instructions": [{k: said[k] for k in ("by", "at", "text")}
                                 for said in result.instructions.get(who.id, [])],
                "linked_from": result.linked_from.get(who.id)}
    out["text"] = article_text(article)
    if result is not None and (since := result.since_written.get(who.id)):
        out["facts_since_written"] = [{k: v for k, v in _fact(f, result.stale, False, result.ties).items()
                                       if k in ("id", "text", "relation", "via", "at", "record", "out_of_date")}
                                      for f in since[:SINCE_WRITTEN]]
        out["more_facts_since_written"] = max(0, len(since) - SINCE_WRITTEN) or None
    return out


def _passage(found: Found) -> str:
    """The part of an article a search matched: its heading and what is written under it, or what is written before
    any heading."""
    article = found.item
    assert isinstance(article, Article)
    found_in = passages(article)
    compilation = found_in[found.place] if 0 <= found.place < len(found_in) else next(
        (s for s in found_in if s.heading == found.heading), None)
    if compilation is None:
        return article.identity.title
    return f"## {compilation.heading}\n\n{compilation.text.strip()}" if compilation.heading else compilation.text.strip()


def _item(item: Any, stale: dict[str, str], ties: dict[str, str] | None = None) -> Any:
    if isinstance(item, Found):
        if item.kind == ARTICLE:
            assert isinstance(item.item, Article)
            if not written(item.item):  # no run wrote it, so no passage: its title is never passed off as one
                return {"kind": ARTICLE, "id": item.id, "title": item.title,
                        "pending": True if pending(item.item) else None,
                        "facts": [_fact(f, stale) for f in item.folded]}
            return {"kind": ARTICLE, "id": item.id, "title": item.title, "passage": _passage(item),
                    "facts": [_fact(f, stale) for f in item.folded]}
        return {**_fact(item.item, stale), "kind": item.kind}  # type: ignore[arg-type]
    if isinstance(item, Article):
        return _article(item)
    if isinstance(item, Fact):
        return _fact(item, stale, ties=ties)
    if isinstance(item, Record):
        return asdict(item)
    return item


def outside(result: Any) -> Any:
    """An operation's result as a caller outside Cerebro gets it."""
    if isinstance(result, ReadResult):
        return pruned({"missing": result.missing, "warnings": result.warnings,
                       "articles": [_article(a, result) for a in result.articles],
                       "facts": [_fact(f, result.stale, ties=result.ties) for f in result.facts]})
    if isinstance(result, QueryResult):
        return pruned({"source": result.source, "count": result.count, "ordered_by": result.ordered_by,
                       "best_first": result.best_first, "warnings": result.warnings, "read_as": result.read_as,
                       "items": [_item(i, result.stale, result.ties) for i in result.items]})
    if isinstance(result, DecideResult):
        return pruned({"answers": result.answers, "notes": result.notes})
    return pruned(asdict(result) if is_dataclass(result) and not isinstance(result, type) else result)
