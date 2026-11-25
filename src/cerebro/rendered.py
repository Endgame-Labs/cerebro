"""Rendered parts: the parts of an agent's article that code writes, each under its heading, with no model.

An agent file's `rendered:` maps a heading to what renders it. Each stands before the agent's own text, in the order `rendered:` lists them, or after it with
`place: after`. The one kind is `collections`: every collection the wiki holds, in order, each title linked to
its folder from the article the part lands on. `style: outline`, the default, is one bullet each, as
`- [Things](../things/) (25 articles)`; `style: headings` gives each a heading of its own, `### [Things](../things/)`,
with what it shows on a line under it. `show` names what each shows beside its title: `count`, how many of its
written articles are in the working set, as `(25 articles)` or `(1 article)`, and `description`, the line the workspace gives it.

A rendered part states only what the store holds, so it cites nothing and is never grounded. It lands under the
agent's rendered mark, `rendered:<agent>`, which no run of the agent rewrites or deletes, and its stamp is the hash of
its text: it is written only when rendering it again gives other text, as after an admit or an archive, so a compile
with nothing changed leaves it and its stamp as they were. A rendered heading the agent no longer names is deleted. A pass also lands the rendered parts of an agent that writes an article of its
own at its end, with no model, so an article admitted or archived shows in them without waiting for the agent's cooldown. This
module reads and writes the store and nothing else: it imports no model, no decider and no extraction.

A collection's description is the line `settings.yaml` gives it, as `collections: { things: { description: Who the
team works with. } }`. A collection it does not name shows none.
"""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .article import Compilation
from .errors import CerebroError

if TYPE_CHECKING:
    from .agent import Agent, Render
    from .store import Store

__all__ = ["MARK", "SETTINGS_KEYS", "Listed", "RenderError", "collections", "descriptions", "land", "mark", "place", "refresh", "render",
           "stamp_of"]

MARK = "rendered:"  # what a rendered compilation's `by` starts with, before the agent's id


class RenderError(CerebroError, ValueError):
    """The lines `settings.yaml` gives the collections do not read. The message names the key and the fix."""


@dataclass
class Listed:
    """One collection as a rendered part lists it: its folder, its title, its description, how many of its articles
    are in the working set, and the link to it from the article the part lands on."""

    collection: str
    title: str
    description: str
    count: int
    link: str


def mark(agent: Agent) -> str:
    """The mark a rendered part of `agent` lands under: the agent's, with no model in it."""
    return MARK + agent.id


def stamp_of(text: str) -> str:
    """A rendered part's stamp: the hash of its text, so it moves only when what it lists does."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _title(collection: str) -> str:
    words = collection.replace("-", " ").replace("_", " ")
    return words[:1].upper() + words[1:]


def _link(article: str, collection: str) -> str:
    """The link from the article whose id is `article` to the folder of `collection`, ending in a slash."""
    folder = posixpath.dirname(article)
    if not folder:
        return f"{collection}/"
    if folder == collection:
        return "./"
    return "../" * (folder.count("/") + 1) + f"{collection}/"


def _plural(n: int, noun: str) -> str:
    return f"1 {noun}" if n == 1 else f"{n} {noun}s"


def collections(store: Store, article_id: str, descriptions: Mapping[str, str] | None = None) -> list[Listed]:
    """Every collection the store holds that has a written article, in its order, with how many of its written articles
    are in the working set: an archived or merged article counts for none, nor does one no run has written, which has no
    copy for a link to reach. `article_id`, the one being rendered, counts as written. `descriptions` gives a collection
    its line, by folder."""
    out = []
    for collection in store.collections():
        shown = [a for a in store.query(collection) if a.compilations or a.identity.id == article_id]  # each has a copy
        if not shown:
            continue
        articles = sum(1 for article in shown if article.identity.status == "active")
        out.append(Listed(collection, _title(collection), (descriptions or {}).get(collection, "").strip(), articles,
                          _link(article_id, collection)))
    return out


def render(spec: Render, listed: list[Listed]) -> str:
    """The body of one rendered part: `listed` laid out as `spec`'s style says, showing what its show says. An
    outline is one bullet each, its count in brackets and its description after a colon. Headings give each a
    level-three heading, then a line with its description and its count, as `What the team sells to. 25 articles.`"""
    count, described = "count" in spec.show, "description" in spec.show
    lines: list[str] = []
    for item in listed:
        articles = _plural(item.count, "article") if count else ""
        about = item.description if described else ""
        if spec.style == "headings":
            if lines:
                lines.append("")
            lines.append(f"### [{item.title}]({item.link})")
            said = ([about.rstrip(".") + "."] if about else []) + ([articles + "."] if articles else [])
            if said:
                lines += ["", " ".join(said)]
            continue
        lines.append(f"- [{item.title}]({item.link})" + (f" ({articles})" if articles else "") + (f": {about}" if about
                                                                                             else ""))
    return "\n".join(lines)


def land(store: Store, agent: Agent, article: str, at: str, descriptions: Mapping[str, str] | None = None) -> list[str]:
    """Write each part `agent`'s `rendered:` names on `article`, in no model's name, and give the headings written.

    A part is written only when rendering it again gives other text than the article holds, stamped with the hash of
    its text, at `at`; one that gives the same text is left, stamp and time, as it was. A part under the agent's
    rendered mark whose heading `rendered:` no longer names is deleted, and a part a run or a person wrote is
    never touched. An article that does not exist yet is left alone: its first compile births it, and the next one
    renders it."""
    current = store.article(article)
    if current is None:
        return []
    by = mark(agent)
    held = {s.heading: s for s in current.compilations if s.by == by}
    shown = headings(store, agent)
    for heading in held:
        if heading not in shown.values():
            store.delete_compilation(article, heading)
    listed = collections(store, article, descriptions)
    written = []
    for heading, spec in agent.rendered.items():
        text = store.landed(Compilation(article, heading, render(spec, listed), by=by))[0].text  # as the store writes it
        old = held.get(shown[heading])
        if old is not None and old.text == text and old.digest == stamp_of(text):
            continue
        store.write_compilation(Compilation(article, heading, text, by=by, digest=stamp_of(text), at=at, started=at))
        written.append(heading)
    place(store, agent, article)
    return written


def headings(store: Store, agent: Agent) -> dict[str, str]:
    """Each heading `rendered:` names, and the heading the store writes it under."""
    from .clean import land_heading

    return {heading: land_heading(heading, store.scrubbing()).text for heading in agent.rendered}


def place(store: Store, agent: Agent, article: str) -> None:
    """Put `agent`'s rendered parts of `article` where `place` says: those before ahead of every other part, those after
    behind them, each in the order `rendered:` lists them. Nothing is written when they stand so already."""
    current = store.article(article)
    if current is None:
        return
    by = mark(agent)
    mine = {s.heading: s for s in current.compilations if s.by == by}
    shown = headings(store, agent)
    before = [mine[shown[h]] for h, spec in agent.rendered.items() if spec.place != "after" and shown[h] in mine]
    after = [mine[shown[h]] for h, spec in agent.rendered.items() if spec.place == "after" and shown[h] in mine]
    want = before + [s for s in current.compilations if s.by != by] + after
    if [s.heading for s in want] == [s.heading for s in current.compilations]:
        return
    with store.exclusive():
        for compilation in want:
            store.delete_compilation(article, compilation.heading)
            store.write_compilation(compilation)


SETTINGS_KEYS = {"collections": {"*": {"description": None}}}  # "*" is any collection's name


def descriptions(settings: Mapping[str, object]) -> dict[str, str]:
    """The line `settings.yaml` gives each collection, by folder, under `collections: <name>: { description }`, its
    spaces folded to one. A collection with no line, or an empty one, is left out. Raises `RenderError` with the
    line that says what to write when the key does not read."""
    raw = settings.get("collections")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RenderError("settings.yaml collections is not a map: write collections: then a line such as "
                          "things: { description: What the team tracks. }")
    out: dict[str, str] = {}
    for name, value in raw.items():
        line = value.get("description", "") if isinstance(value, dict) else None
        if not isinstance(line, str) or set(value) - {"description"}:
            raise RenderError(f"settings.yaml collections.{name} takes description alone: write "
                              f"{name}: {{ description: <one line> }}")
        if line.strip():
            out[str(name)] = " ".join(line.split())
    return out


def refresh(store: Store, agents: list[Agent], at: str, descriptions: Mapping[str, str] | None = None
            ) -> dict[str, list[str]]:
    """Land the rendered parts of each agent that writes an article of its own on that article, as `land` does,
    and give the headings written by article. A pass calls it at its end, so what curators admitted and
    archived shows with no model and no wait for the agent's cooldown."""
    written: dict[str, list[str]] = {}
    for agent in agents:
        if agent.role == "writer":
            headings = land(store, agent, agent.target, at, descriptions)
            if headings:
                written[agent.target] = headings
    return written
