"""`cerebro review` at a terminal: the proposals of articles awaiting a person's review, as the first run's tree.

The tree is the first run's, made the same way from the admits that wait for this person. Every other proposal of an
article, an archive, a merge, a split, or an admit no curator of the collection filed, is a row of its own, saying what
accepting it would change. Approve accepts what is ticked through the first run's own approval, and what is not ticked
waits. `r` on a row rejects it with a reason, through `decide` as the person deciding. A proposal of no article is not
on the screen: the walk asks about it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .caller import Caller
from .first_run import Applied, Choices, Listed, LocalApproval, Pile, arranged, decided_meanwhile, offer
from .records import REJECTED
from .review import Walked, _refusal
from .tree import Group, Row

if TYPE_CHECKING:
    from .app import Cerebro

__all__ = ["ReviewApproval", "ended", "others", "screen"]


def _article(kind: str) -> bool:
    return kind.startswith("article.")


class ReviewApproval(LocalApproval):
    """The proposals of articles awaiting review by `caller`, as the first run's tree shows a pile."""

    def __init__(self, workspace: Cerebro, caller: Caller):
        super().__init__(workspace, caller, agents=[])  # no curator is asked, so no left-out row is listed

    def pile(self) -> Pile:
        """`_listed` as a screen shows it, masked as the first run's list is, each row a group of its own."""
        pile = self.workspace.masked(self._listed())
        pile.groups = [Group([row]) for row in pile.rows()]
        return pile

    def _listed(self) -> Pile:
        """The first run's pile of the admits that wait for the caller, then each other proposal of an article."""
        mine = {item.proposal.id: item for item in self.workspace.review(self.caller) if _article(item.proposal.kind)}
        base = super()._listed()
        lists = [Listed(listed.agent, listed.collection, [row for row in listed.rows if row.id in mine])
                 for listed in base.lists]
        shown = {row.id for listed in lists for row in listed.rows}
        other = Listed("", "")
        for id, item in mine.items():
            if id not in shown:
                p = item.proposal
                other.rows.append(Row(id, p.by.name, p.target.partition("/")[0], p.target,
                                      f"{item.change}; why: {p.reason or 'no reason given'}", path=p.target,
                                      kind=p.kind))
        return Pile([listed for listed in [*lists, other] if listed.rows], review=True)

    def apply(self, choices: Choices) -> Applied:
        """Accept what is ticked, as the first run's list does, and with nothing ticked, decide nothing: either way
        what is left starts unticked again."""
        if choices.accept:
            return super().apply(choices)
        offer(self.workspace, self._listed().rows())
        return Applied()

    def reject(self, group: Group, reason: str) -> tuple[str, str]:
        """Reject every row of one group with the person's reason: `decide`'s answer and note for its lead."""
        said = self.workspace.call(self.caller, "decide", {"reject": [{"id": id, "reason": reason} for id in group.ids()]})
        lead = group.ids()[0]
        return (said or {}).get("answers", {}).get(lead, ""), (said or {}).get("notes", {}).get(lead, "")


def others(workspace: Cerebro, caller: Caller) -> int:
    """How many proposals awaiting review by `caller` are of no article, so the screen leaves them to the walk."""
    return sum(not _article(item.proposal.kind) for item in workspace.review(caller))


def screen(workspace: Cerebro, caller: Caller,
           draw: Callable[[Pile, Callable[[Group, str], bool]], Choices | None]) -> Walked | None:
    """Show the proposals of articles awaiting review by `caller` with `draw`, the first run's screen, and decide what
    it gives: each reject as it is made, then what Approve ticked. None when no proposal of an article waits."""
    approval = ReviewApproval(workspace, caller)
    pile = approval.pile()
    if not pile.rows():
        return None
    pile = arranged(workspace, pile)
    walked = Walked()

    def reject(group: Group, reason: str) -> bool:
        answer, note = approval.reject(group, reason)
        id = group.ids()[0]
        if answer == "decided":
            walked.rejected.append(id)
            walked.notes[id] = note
            return True
        if stood := decided_meanwhile(workspace, [id], REJECTED):
            walked.notes |= {i: line for i, line in stood.items() if line}
            return True
        walked.refused[id] = _refusal(answer, note, id)
        return False

    choices = draw(pile, reject)
    if choices is not None:
        applied = approval.apply(choices)
        walked.accepted += applied.accepted
        walked.notes |= {id: applied.notes[id] for id in applied.accepted if id in applied.notes} | applied.already
        walked.refused |= applied.refused
    walked.left = [item.proposal.id for item in workspace.review(caller)]
    return walked


def ended(walked: Walked, others: int) -> list[str]:
    """The screen's last lines: each decision's own line, an accept said as approved, as the screen's action is, each
    refusal, the proposals of no article left to the walk, then what was approved and rejected and how many wait."""
    decided = walked.rejected + walked.accepted
    out = [walked.notes[id].replace(f"Accepted {id}", f"Approved {id}", 1) for id in decided if id in walked.notes]
    out += [line for id, line in walked.notes.items() if id not in decided]  # decided the other way meanwhile
    out += [f"refused  {line}" for line in walked.refused.values()]
    if others:
        out.append(f"waiting   {others} proposal{'s' if others > 1 else ''} of no article "
                   f"{'are' if others > 1 else 'is'} waiting: cerebro review --plain asks about "
                   f"{'them' if others > 1 else 'it'} one at a time")
    done = f"Done: {len(walked.accepted)} approved, {len(walked.rejected)} rejected"
    done += f", {len(walked.refused)} refused." if walked.refused else "."
    if still := len(walked.left):
        done += f" {still} is still waiting." if still == 1 else f" {still} are still waiting."
    return [*out, done]
