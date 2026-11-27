"""A list of proposed articles as a person decides it: a tree that one model call arranges.

The rows are the proposals that wait, and the candidates a curator left out. Proposals that share a path are one
item. One call to the writing model, on the bundled `first-list` prompt, puts together the items of one collection
that are the same thing, orders the groups and says which to tick, with the person's own words, when they gave
any, as their instruction. Code checks each group: none across collections, none that repeats an item, and none
whose members hold different values of one identity key. A group that fails stays separate items where it stood, no
two of them ticked, and one line says so when its items may be other things. An id no item has is dropped, and an
item the order leaves out comes last, unticked. A reply that is not the JSON asked for or orders no item, or no
model, gives the plain tree: the order the asks were filed in, nothing ticked, and one line that says why.

The tree comes from the data: a group sits under the group or the article its `linked` names, the first one when it
names several. A group under nothing with nothing under it is not grouped. Every string from a model or a record goes
to the model as data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import CerebroError
from .identity import follow
from .shown import shown

if TYPE_CHECKING:
    from .app import Cerebro

PROMPT = "first-list"
REASON_CAP = 300  # characters of a proposal's reason the model reads
KEPT_TWO = "Kept apart: {a} and {b} may not be the same thing, so each is shown on its own."
KEPT_NAMED = "Kept apart: {n} proposals named {a} may not be the same thing, so each is shown on its own."
KEPT_MORE = "Kept apart: {a} and {n} others may not be the same thing, so each is shown on its own."
KEPT_GROUPS = "Kept apart: {n} groups that may not be the same thing, so each item is shown on its own."


@dataclass
class Row:
    """One proposal that waits, by its id, or a candidate a curator left out, by `left-out:<seq>` or
    `not-weighed:<source>:<id>`. `agent` filed it, `path` is the article it would make, and `linked` the paths it
    belongs under."""

    id: str
    agent: str
    collection: str
    name: str
    reason: str = ""
    left_out: bool = False
    keys: dict[str, list[str]] = field(default_factory=dict)
    record: dict[str, str] | None = None
    path: str = ""
    linked: list[str] = field(default_factory=list)
    offered: bool = False  # left unticked on an earlier list, so it starts unticked
    own: bool = False  # of the workspace's own side, which the row says
    kind: str = "article.admit"
    unweighed: bool = False  # Cerebro's own row for a held record its curator never weighed


@dataclass
class Group:
    """The rows that make one article, the lead's first, whether it is ticked, and the groups that sit under it. A
    group with no rows is an article of the working set that others sit under."""

    rows: list[Row]
    tick: bool = False
    under: list[Group] = field(default_factory=list)
    over: Group | None = None  # the group or article this one sits under, as its `linked` names it
    article: tuple[str, str] = ("", "")  # the id and title of an article of the working set, for a group of no rows

    @property
    def lead(self) -> Row | None:
        return self.rows[0] if self.rows else None

    @property
    def name(self) -> str:
        return self.lead.name if self.lead else self.article[1] or self.article[0]

    @property
    def collection(self) -> str:
        return self.lead.collection if self.lead else self.article[0].rpartition("/")[0]

    def ids(self) -> list[str]:
        lead = self.lead
        return [r.id for r in sorted(self.rows, key=lambda r: r is not lead)]


def _ask(row: Row) -> dict[str, Any]:
    return {"keys": row.keys, "record": row.record}


def items(rows: list[Row]) -> list[list[Row]]:
    """The proposals of one article, each item once, in the order they were filed: one path of one kind, but asks of
    it that are other things, as `curate.one_thing` says, are items of their own, as a decide makes them."""
    from .curate import one_thing

    out: list[list[Row]] = []
    for row in rows:
        same = next((item for item in out if (item[0].kind, item[0].path or item[0].id) == (row.kind, row.path or row.id)
                     and all(one_thing(_ask(r), _ask(row)) for r in item)), None)
        if same is None:
            out.append([row])
        else:
            same.append(row)
    return out


def plain(rows: list[Row]) -> list[Group]:
    """Every item a group of its own, in the order filed, nothing ticked."""
    return [Group(item) for item in items(rows)]


def _data(found: list[list[Row]]) -> list[dict[str, Any]]:
    """What the model reads of each item."""
    out = []
    for n, item in enumerate(found, 1):
        keys: dict[str, list[str]] = {}
        for row in item:
            for k, vs in row.keys.items():
                keys[k] = [*keys.get(k, []), *(v for v in vs if v not in keys.get(k, []))]
        one: dict[str, Any] = {"id": f"n{n}", "collection": item[0].collection, "path": item[0].path,
                               "name": Group(item).name}
        if keys:
            one["keys"] = keys
        if linked := list(dict.fromkeys(p for row in item for p in row.linked)):
            one["linked"] = linked
        one["proposals"] = [{"by": row.agent, "reason": row.reason[:REASON_CAP]} for row in item]
        out.append(one)
    return out


def checked(text: str, found: list[list[Row]]) -> tuple[list[Group] | None, str]:
    """The groups a reply gives, in its order, and a line on the groups it put together that may be other things, or
    None with why the whole reply is refused. A group it refuses stays separate items at the place the group held, and
    no two of them are ticked."""
    from .curate import one_thing
    from .first_run import NAME_CAP

    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        reply = json.loads(match.group(0)) if match else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or not all(isinstance(reply.get(k), list) for k in ("same", "order", "tick")):
        return None, "the model's reply is not the JSON asked for"
    by_id = {f"n{n}": item for n, item in enumerate(found, 1)}

    def known(part: Any) -> list[str]:
        return [i for i in (part if isinstance(part, list) else [part]) if isinstance(i, str) and i in by_id]

    group_of = {id: (id,) for id in by_id}
    refused: list[list[str]] = []
    said: list[list[str]] = []
    for same in reply["same"]:
        ids = known(same)
        rows = [row for i in ids for row in by_id[i]]
        if len({*ids}) != len(ids) or any(len(group_of[i]) > 1 for i in ids):
            refused.append(sorted({*ids}, key=lambda i: int(i[1:])))
        elif len({row.collection for row in rows}) > 1 or not all(one_thing(_ask(a), _ask(b)) for a in rows for b in rows):
            refused.append(sorted(ids, key=lambda i: int(i[1:])))
            if refused[-1] not in said:
                said.append(refused[-1])
        elif len(ids) > 1:
            for i in ids:
                group_of[i] = tuple(ids)
    split: dict[str, list[str]] = {}
    together: dict[str, set[str]] = {i: set() for i in by_id}
    for ids in refused:
        alone = [i for i in ids if len(group_of[i]) == 1]
        for i in alone:
            split.setdefault(i, alone)
        for i in ids:
            together[i] |= {*ids} - {i}
    order: list[tuple[str, ...]] = []
    for i in known(reply["order"]):
        order += [g for g in ([(j,) for j in split[i]] if i in split else [group_of[i]]) if g not in order]
    if not order:
        return None, "the model's reply orders no item"
    ticked: set[tuple[str, ...]] = set()
    taken: set[str] = set()
    for i in sorted(known(reply["tick"]), key=lambda i: i in split):  # a group that stands keeps its tick
        g = (split[i][0],) if i in split else group_of[i]
        if g in order and (i not in split or not any(together[j] & taken for j in g)):
            ticked.add(g)
            taken |= {*g}
    order += [g for g in dict.fromkeys(group_of.values()) if g not in order]
    groups = [Group([row for i in ids for row in by_id[i]], ids in ticked) for ids in order]
    if len(said) != 1:
        return groups, KEPT_GROUPS.format(n=len(said)) if said else ""
    names = [shown(" ".join(Group(by_id[i]).name.split()), NAME_CAP) for i in said[0]]
    return groups, (KEPT_NAMED.format(n=len(names), a=names[0]) if len({*names}) == 1 else
                    KEPT_TWO.format(a=names[0], b=names[1]) if len(names) == 2 else
                    KEPT_MORE.format(a=names[0], n=len(names) - 1))


def arrange(workspace: Cerebro, rows: list[Row], line: str = "",
            asking: Callable[[], None] = lambda: None) -> tuple[list[Group], str]:
    """The groups of `rows` as the model arranges them, with the person's `line`, or the plain ones and why; `asking`
    runs just before the model is asked. Only proposals that make an article go to the model: any other row keeps
    its place after them, a bind ticked as what it binds is."""
    found = items([row for row in rows if row.kind == "article.admit" and row.path and not row.left_out])
    rest = plain([row for row in rows if not (row.kind == "article.admit" and row.path) and not row.left_out])
    if not found:
        return rest, ""
    request = json.dumps({"instruction": line, "items": _data(found)}, ensure_ascii=False)
    asking()
    try:
        groups, why = checked(workspace.ask_model(PROMPT, request, job="compilation"), found)
    except CerebroError as error:
        groups, why = None, f"the model that arranges it did not answer: {error}"
    if groups is None:
        return [*plain([row for item in found for row in item]), *rest], f"Shown in the order filed, nothing ticked: {why}."
    ticked = {row.path for g in groups if g.tick for row in g.rows}
    for g in rest:
        g.tick = all(row.kind == "article.admit" and row.linked[:1] and row.linked[0] in ticked for row in g.rows)
    return [*groups, *rest], why


def tree(groups: list[Group], articles: dict[str, str], moved: dict[str, str]) -> list[Group]:
    """The top-level groups in the order given, each with what sits under it: a group sits under the group or the
    article of `articles`, id to title, that its `linked` names, or a merge in `moved`, as `redirects` gives it, leads
    it to, the first one when it names several, and under what that one sits under."""
    at: dict[str, Group] = {}
    for g in groups:
        for row in g.rows:
            if row.path and row.kind == "article.admit":
                at.setdefault(row.path, g)
    made: dict[str, Group] = {}

    def up(g: Group) -> Group | None:
        for path in (follow(p, moved) for row in g.rows for p in row.linked):
            if (found := at.get(path)) is not None and found is not g:
                return found
            if path in articles and path not in at:
                return made.setdefault(path, Group([], article=(path, articles[path])))
        return None

    place = {id(g): n for n, g in enumerate(groups)}

    def top(g: Group) -> Group:
        chain = [g]
        while (above := up(chain[-1])) is not None:
            if any(above is c for c in chain):  # a ring: its first group in the order given is its top
                ring = chain[next(n for n, c in enumerate(chain) if c is above):]
                return min(ring, key=lambda c: place.get(id(c), len(place)))
            chain.append(above)
        return chain[-1]

    tops = [g for g in groups if top(g) is g]
    for g in groups:
        if (t := top(g)) is not g:
            if t not in tops:
                tops.append(t)
            t.under.append(g)
            g.over = up(g)
    return sorted(tops, key=lambda g: not g.under)  # what has nothing under it comes last, as not grouped
