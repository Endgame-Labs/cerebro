"""How a live check approves a first run's lists: the list printed as `--plain` shows it, then what it ticks, as
`--yes` takes it. The first list is kept in `FIRST`, so a check fails in one line when it ticked nothing, and says
how many people and calls sit under the right account."""

from __future__ import annotations

import re
from typing import Any

from cerebro.first_run import Choices, Pile, lines

FIRST: dict[str, Any] = {}  # the first list and how many articles it ticked


def whole(pile: Pile) -> Choices:
    print("\n".join(lines(pile)))
    choices = Choices.starter(pile)
    if not pile.late:
        FIRST.update(pile=pile, selected=len(choices.accept))
        print(f"defaults  {len(choices.accept)} ticked" + (f"; {pile.note}" if pile.note else ""))
    return choices


def taken_whole(pile: Pile) -> Choices:
    """As `whole`, then as a person who takes each group the model ticked whole: every line under a ticked top line is
    ticked too, and one line says how many more that ticked."""
    ticked = whole(pile)
    groups = [g for top in pile.groups if top.tick for g in (top, *top.under) if g.rows]
    choices = Choices.of(pile, groups + [g for top in pile.groups if not top.tick for g in (top, *top.under) if g.tick])
    if not pile.late:
        print(f"whole     {len(choices.accept) - len(ticked.accept)} more ticked under the ticked lines")
    return choices


def _folded(name: str) -> str:
    return " ".join(name.casefold().split())


def placed(pile: Pile, notes: dict[str, str], own_domain: str) -> str:
    """How many people and calls of the list sit under the account its notes give them, and how many accounts are
    ticked: each note's `Account:` line keys its call and its attendees at an outside address."""
    account = {rel: found.group(1).strip() for rel, text in notes.items()
               if (found := re.search(r"(?m)^Account:\s*(.+)$", text))}
    person = {_folded(name): account[rel] for rel, text in notes.items() if rel in account
              for name, address in re.findall(r"([^,<>\n:]+?)\s*<([^>\s]+)>", text)
              if not address.lower().endswith("@" + own_domain)}
    counts = {"people": [0, 0], "calls": [0, 0]}
    for top in pile.groups:
        for g in (top, *top.under):
            if g.collection not in counts:
                continue
            truth = person.get(_folded(g.name)) if g.collection == "people" else next(
                (account[r.record["id"]] for r in g.rows if r.record and r.record.get("id") in account), None)
            counts[g.collection][1] += 1
            counts[g.collection][0] += g is not top and truth is not None and _folded(truth) == _folded(top.name)
    accounts = [g for g in pile.groups if g.collection == "accounts"]
    return (f"people {counts['people'][0]} of {counts['people'][1]} and calls {counts['calls'][0]} of "
            f"{counts['calls'][1]} under the right account; accounts ticked {sum(g.tick for g in accounts)} of "
            f"{len(accounts)}")
