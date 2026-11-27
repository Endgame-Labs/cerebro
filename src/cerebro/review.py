"""A person's review: each proposal awaiting review, what accepting it would change, who filed it, when and why,
and an answer for each, accept, reject or skip.

The functions here return data, and the prompt and the printing are handed in, so the command line holds no logic
and a test walks a review with a list of answers. Every decision goes through the app's own `review` and `decide`,
as the caller given, so each rule about who may decide what holds here as it does over MCP. A walk decides each
proposal as it is answered, so an answer stands if the walk stops after it. With no prompt, as off a terminal, a walk
asks nothing: it refuses in one line that names the flags.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .caller import Caller, describe
from .connector import one_line
from .errors import CerebroError
from .identity import would_bind
from .records import DecideResult, Decision, Review, proposals
from .store import record_from, scrubbed

if TYPE_CHECKING:
    from .app import Cerebro

__all__ = [
    "ANSWERS",
    "TRIES",
    "Answer",
    "NoSuchAgent",
    "NotATerminal",
    "Walked",
    "agent_named",
    "binds",
    "filed_by",
    "lines",
    "parse",
    "refused",
    "result_lines",
    "row",
    "summary",
    "waiting",
    "walk",
]

ACCEPT, REJECT, SKIP, QUIT = "accept", "reject", "skip", "quit"
ANSWERS = {"a": ACCEPT, "accept": ACCEPT, "r": REJECT, "reject": REJECT, "s": SKIP, "skip": SKIP, "": SKIP,
           "q": QUIT, "quit": QUIT}
TRIES = 3  # answers a walk takes for one proposal before it leaves that one awaiting review
FLAGS = "decide with --accept <id> or --reject <id>, each repeatable, or list them with --json"
HELP = "answer accept, reject, skip or quit, or a, r, s or q, with a reason after it if you like, as r: kept already"


class NoSuchAgent(CerebroError):
    """A file `review --accept-from` names that is no agent of the workspace."""


class NotATerminal(CerebroError):
    """A review that would ask a person, with no terminal to ask on."""


@dataclass
class Answer:
    """A person's answer to one proposal: accept, reject, skip or quit, and the reason given with it."""

    choice: str
    reason: str = ""


@dataclass
class Walked:
    """What a walk did: the ids accepted, rejected and skipped, those still awaiting review because the walk stopped,
    the ids whose decision was refused with the line that says why, and the line `decide` gave each decided id."""

    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)


def parse(text: str) -> Answer | None:
    """An answer as typed: its first word is the choice, and the rest, after a colon if one is there, the reason.
    An empty answer skips. None when the first word is no choice."""
    head, _, rest = text.strip().partition(" ")
    head, _, after = head.partition(":")
    choice = ANSWERS.get(head.lower())
    if choice is None:
        return None
    return Answer(choice, f"{after} {rest}".strip().lstrip(":").strip())


def row(item: Review, held: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One proposal awaiting review as data: its id, kind and target, what accepting it would change, who filed it
    and when, its reason, what it cites, what it carries, and under `binds` the held records an admit's keys would
    bring in, as `binds` gives them."""
    p = item.proposal
    return {"id": p.id, "kind": p.kind, "target": p.target, "title": item.title, "change": item.change,
            "by": {"name": p.by.name, "kind": p.by.kind}, "at": p.at, "reason": p.reason, "cites": list(p.cites),
            "payload": dict(p.payload), "binds": list(held or [])}


def binds(workspace: Cerebro, item: Review) -> list[dict[str, Any]]:
    """The records the sources hold back that an admit would bind and bring in once made, by the keys and name it
    asks for, each with its source, id and fields, such as a status and a date, so a person sees what the article would be about. Masked of every
    value the secrets store holds, since a held record's fields were never masked by a read of their own."""
    if item.proposal.kind != "article.admit":
        return []
    payload = item.proposal.payload
    keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    keys = {str(k): v for k, v in keys.items() if isinstance(v, list)}
    title = str(payload.get("name") or payload.get("title") or "")
    if not keys and not title:
        return []
    found = []
    for source in sorted(workspace.connectors):
        for row in workspace.store.state(f"held:{source}") or []:
            held = record_from({"source": source, "revision": "", **row})  # with the keys it only carries
            if would_bind(workspace.store, keys, title, held):  # as the store binds: by keys, any case, or by title
                found.append({"source": source, "id": row["id"], "fields": row.get("fields") or {}})
    return scrubbed(found, workspace.mask_every())  # every secret the store holds, read yet or not


MAX_ACCEPTED = 10  # accepted proposals not carried out yet that a list names, one a line


def held_lines(workspace: Cerebro) -> list[str]:
    """A line for each accepted proposal its curator has not carried out yet, and the line that counts the proposals
    a person is not asked about because a curator holds them, or none."""
    from .curate import carried_out

    left = [p for p in proposals(workspace.store, "accepted") if not carried_out(p)]
    lines = [workspace.mask_every()(f"{p.id} {p.kind.partition('.')[2]} of {p.target}: accepted, not carried out "
                                    "yet: its curator carries it out on its next run") for p in left[:MAX_ACCEPTED]]
    if len(left) > MAX_ACCEPTED:
        lines.append(f"and {len(left) - MAX_ACCEPTED} more accepted, not carried out yet")
    return lines + _with_curators(workspace)


def _with_curators(workspace: Cerebro) -> list[str]:
    """The line that counts the proposals a person is not asked about because a curator holds them, or none."""
    if not (n := len(workspace.with_curators())):
        return []
    see = """cerebro tools query --json '{"from": "proposals", "status": "waiting"}'"""
    if n == 1:
        return [(f"1 proposal waits with its collection's curator, so it is not listed: decide it with cerebro review "
                 f"--accept ID or --reject ID, or see it with {see}")]
    return [(f"{n} proposals wait with their collections' curators, so they are not listed: decide one with cerebro "
             f"review --accept ID or --reject ID, or list them with {see}")]


def waiting(workspace: Cerebro, caller: Caller) -> list[dict[str, Any]]:
    """The proposals awaiting review by `caller`, oldest first, as `row` gives each, read through the app's review."""
    return [row(item, binds(workspace, item)) for item in workspace.review(caller)]


def lines(item: Review, n: int, of: int, held: list[dict[str, Any]] | None = None) -> list[str]:
    """How one proposal shows to a person: its id and place, its kind and target, the name and keys an admit gives,
    what it would change, who filed it and when, why, what it cites, and each held record it would bring in."""
    p = item.proposal
    shown = [f"{p.id}  {n} of {of}, awaiting review",
             f"  {p.kind.removeprefix('article.').removeprefix('record.'):<8} {p.target}"]
    if item.title:
        shown.append(f"  title    {one_line(item.title)}")
    if name := p.payload.get("name"):
        shown.append(f"  name     {one_line(name)}")
    if words := p.payload.get("instructions"):
        shown.append(f"  instruct {one_line(words, 600)}")
    if isinstance(keys := p.payload.get("keys"), dict) and keys:
        shown.append("  keys     " + one_line("; ".join(f"{k} {', '.join(map(str, v or []))}"
                                                     for k, v in keys.items())))
    shown += [f"  change   {item.change}",
             f"  filed    by {describe(p.by) if p.by.name else 'an unnamed ' + p.by.kind}"
             + (f", at {p.at}" if p.at else ""),
             f"  why      {p.reason or 'no reason given'}"]
    if p.cites:
        shown.append(f"  cites    {', '.join(p.cites)}")
    for record in held or []:
        said = ", ".join(f"{k} {v}" for k, v in record["fields"].items() if v not in (None, "", [], {}))
        shown.append(f"  binds    {record['source']} {record['id']}" + (f": {one_line(said, 160)}" if said else ""))
    return shown


def _refusal(answer: str, note: str, id: str = "") -> str:
    """The one line a decision `decide` did not make gets: its note, and for one already decided or an id no proposal
    has, the command a person runs to see what is awaiting review."""
    if answer == "already decided":
        return f"{note}, so it stands as it was: cerebro review --json lists what is awaiting review"
    if answer == "unknown":
        return f"there is no proposal {id!r}: cerebro review --json lists the ids awaiting review"
    return note


def _ask(ask: Callable[[str], str], say: Callable[[str], None], id: str) -> Answer:
    for _ in range(TRIES):
        answer = parse(ask(f"Accept, reject or skip {id}? [a/r/s/q]"))
        if answer is not None:
            return answer
        say(HELP)
    say(f"{id} is skipped after {TRIES} answers that were not a choice")
    return Answer(SKIP)


def walk(workspace: Cerebro, caller: Caller, ask: Callable[[str], str] | None, say: Callable[[str], None]) -> Walked:
    """Show each proposal awaiting review by `caller`, take an answer for each from `ask`, and decide it through
    the app as it is answered. A skip leaves it awaiting review, and a quit leaves it and every one after it.

    With no `ask`, as off a terminal, nothing is asked: a review with proposals awaiting it says each, its id
    included so it can be named with a flag, then refuses in one line that names the flags; one with none returns an
    empty walk."""
    found = workspace.review(caller)
    walked = Walked()
    if not found:
        return walked
    if ask is None:
        for n, item in enumerate(found, 1):
            for line in lines(item, n, len(found), binds(workspace, item)):
                say(line)
        for line in held_lines(workspace):
            say(f"waiting   {line}")
        count = f"{len(found)} proposal{'' if len(found) == 1 else 's'}"
        raise NotATerminal(f"{count} {'is' if len(found) == 1 else 'are'} awaiting review, and input is not a "
                           f"terminal, so nothing was asked: {FLAGS}")
    decided = False
    for n, item in enumerate(found, 1):
        id = item.proposal.id
        if decided:  # as it stands after the rows decided before it
            now = next((later for later in workspace.review(caller) if later.proposal.id == id), None)
            if now is None:
                row = next(p for p in proposals(workspace.store) if p.id == id)
                say(f"{id}  {n} of {len(found)}, {row.status} since: {row.decision_reason or 'decided elsewhere'}")
                continue
            item = now
        for line in lines(item, n, len(found), binds(workspace, item)):
            say(line)
        answer = _ask(ask, say, id)
        if answer.choice == QUIT:
            walked.left = [later.proposal.id for later in found[n - 1:]]
            break
        if answer.choice == SKIP:
            walked.skipped.append(id)
            continue
        decision = [Decision(id, answer.reason)]
        result = workspace.decide(caller, accept=decision if answer.choice == ACCEPT else None,
                              reject=decision if answer.choice == REJECT else None)
        said, note = result.answers[id], result.notes[id]  # decide answers each id it is given
        if said == "decided":
            decided = True
            (walked.accepted if answer.choice == ACCEPT else walked.rejected).append(id)
            walked.notes[id] = note
            say(note)
        else:
            walked.refused[id] = _refusal(said, note, id)
            say(f"refused  {walked.refused[id]}")
    return walked


def summary(walked: Walked) -> str:
    """The last line of a walk: what was decided, and what is still awaiting review."""
    still = len(walked.skipped) + len(walked.left)
    if not (walked.accepted or walked.rejected or walked.refused or still):
        return "Nothing is awaiting review."
    done = f"Done: {len(walked.accepted)} accepted, {len(walked.rejected)} rejected"
    if walked.refused:
        done += f", {len(walked.refused)} refused"
    if not still:
        return done + "."
    return done + f", {still} still awaiting review: run cerebro review again to decide {'it' if still == 1 else 'them'}."


def standing(workspace: Cerebro, result: DecideResult, accept: list[str], reject: list[str]) -> dict[str, str]:
    """The ids of a decide by id that stood already as asked, each with its state: one accepted that `accept` names,
    or one rejected that `reject` names. There is nothing to do for them, and no refusal."""
    state = {p.id: p.status for p in proposals(workspace.store)}
    asked = dict.fromkeys(accept, "accepted") | dict.fromkeys(reject, "rejected")
    return {id: state[id] for id, said in result.answers.items()
            if said == "already decided" and id in asked and state.get(id) == asked[id]}


def refused(result: DecideResult, stood: Mapping[str, str] | None = None) -> list[str]:
    """The ids of a decide that were refused, but those that `stood` already as asked, and those not decided only
    because another was refused."""
    return [id for id, said in result.answers.items() if said not in ("decided", "not decided") and id not in (stood or {})]


def result_lines(result: DecideResult, stood: Mapping[str, str] | None = None) -> list[str]:
    """A decide by id in lines: the note of each id decided, a line for each that `stood` already as asked, and one
    refusal line for each other that was not, the last of them saying that nothing was applied when no id was."""
    stood = stood or {}
    out = [result.notes[id] if said == "decided" else f"{id} is already {stood[id]}, nothing to do" if id in stood
           else f"refused  {_refusal(said, result.notes[id], id)}" for id, said in result.answers.items()]
    if refused(result, stood) and "decided" not in result.answers.values():  # nothing decided: the last refusal says so
        last = max(i for i, line in enumerate(out) if line.startswith("refused  "))
        out[last] += ". Nothing was applied."
    return out


def agent_named(workspace: Cerebro, file: str, flag: str = "--accept-from") -> str:
    """The id of the agent `file` names, such as agents/things/curator.md, as `review --accept-from` or `flag` names
    it. Refused for a file that is no agent of the workspace."""
    agent = file.removeprefix("./").removeprefix("agents/").removesuffix(".md")  # spaces kept
    if not any(a.id == agent for a in workspace.agents):
        raise NoSuchAgent(f"review {flag} {file}: agents/{agent}.md is no agent of this workspace: name "
                          "one cerebro agents list shows, such as agents/things/curator.md")
    return agent


def filed_by(workspace: Cerebro, caller: Caller, file: str, until: str = "", flag: str = "--accept-from") -> list[str]:
    """The ids of the proposals awaiting review by `caller` that the agent of `file` filed, oldest first, and with
    `until`, a stamp, only those filed by then. Refused for a file that is no agent of the workspace."""
    agent = agent_named(workspace, file, flag)
    return [item.proposal.id for item in workspace.review(caller) if item.proposal.by.name == agent
            and (not until or item.proposal.at <= until)]
