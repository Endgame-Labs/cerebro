"""One live check of re-ranking on the decision model: a made-up workspace of a dozen articles, and six questions that
each have one right article. For each question it prints the right article's rank in a search across everything, without
re-ranking and with it. Not part of the test suite: on a real model it calls the decision API and costs a fraction
of a cent.

    TYPESAFE_API_KEY=your-key uv run python checks/live_rerank.py
    uv run python checks/live_rerank.py --fake  # a smoke test on a scripted stand-in, with no model and no network

The decision model is the workspace's default one, so the
key it needs is that provider's. The workspace is a temporary folder with `search: { rerank: true }` in its settings,
and its articles are written into its store directly, with no pass. It has no facts layer of its own: a stand-in that
holds no fact, so only articles rank. It never prints a key. It prints each question with both ranks, the mean
reciprocal rank each way, how many ranks re-ranking made better and worse, what the re-ranking spent, then PASS, or
FAIL with every reason. It passes when every re-ranked search came back re-ranked, with no note, and re-ranking put
no right article lower than the search did.

With `--fake`, the decision model is a stand-in that scores a passage by how many of the question's words it holds,
and each of its calls is counted.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from cerebro import Caller
from cerebro.app import Cerebro
from cerebro.article import Compilation, Identity
from cerebro.budget import LOG
from cerebro.fakes import FakeDecider, FakeExtraction
from cerebro.models import Decision, Question

YOU = Caller("live-rerank", "person")
SETTINGS = "budget:\n  rerank_daily_usd: 0.5\nsearch:\n  rerank: true\n  rerank_top: 20\n"
# A made-up workplace wiki: every article shares words with others, and each question has an article that uses its words
# without answering it, so a word search alone ranks the wrong one first.
ARTICLES = {
    "office-move": ("Office move", ("The team moves to the new office on the third floor in spring. Desks are packed "
                                   "the Friday before the move.")),
    "desk-booking": ("Desk booking", ("Desks are booked a week ahead at the front desk. A desk left empty by noon is "
                                     "freed for the day.")),
    "parking": ("Parking", "Parking passes are renewed each spring. The garage closes at ten at night."),
    "bike-storage": ("Bike storage", ("Bikes are kept in the locked cage beside the garage. The cage code changes "
                                     "each month.")),
    "expense-policy": ("Expense policy", ("Meals on a trip are paid back up to forty dollars a day. Receipts go in "
                                         "within thirty days of the trip.")),
    "travel-booking": ("Travel booking", ("Flights are booked through the travel desk at least two weeks ahead. "
                                         "Trains need no approval.")),
    "laptop-refresh": ("Laptop refresh", ("Laptops are replaced every three years. An old laptop is wiped and handed "
                                         "back to the help desk.")),
    "help-desk": ("Help desk", ("The help desk answers within a day. A lost badge or laptop is reported to the help "
                               "desk at once.")),
    "onboarding": ("Onboarding", ("A new hire gets a laptop, a badge and a desk on the first day. The first week holds "
                                 "three trainings.")),
    "badge-access": ("Badge access", ("Badges open the front doors from seven in the morning to seven at night. After "
                                     "hours a badge needs a manager's approval.")),
    "holiday-calendar": ("Holiday calendar", ("The office closes for eleven days a year. Next year's calendar is set "
                                             "in November.")),
    "team-offsite": ("Team offsite", ("The offsite is held each autumn. Travel to it is booked by the office team, "
                                     "and meals there are paid for.")),
    # Articles that use a question's own words and do not answer it, so a word search ranks them above the right one.
    "door-repairs": ("Door repairs", ("Front doors that stick at night are reported to facilities. What time a "
                                     "repair visit comes is posted on the front doors, and a lock is oiled then.")),
    "flight-tracking": ("Flight tracking", ("How far a flight has come is on the screen by the lifts. A booked "
                                           "flight shows there ahead of time.")),
    "closing-up": ("Closing up", ("The last one out closes the office: lights off, windows closed, and the year "
                                 "planner in the office closed for the night.")),
    "computer-logins": ("Computer logins", ("What happens after three wrong passwords: the computer locks for an "
                                           "hour. Login records are kept for three years.")),
    "lockers": ("Lockers", ("Lockers are locked up with your own lock. Where a locker is free, a bicycle helmet "
                           "can be kept in it.")),
    "event-photos": ("Event photos", ("Photos at the autumn event are paid for by the office, and the event "
                                     "photographer shares them a week later.")),
}
QUESTIONS = {  # each question's one right article, asked in other words than it uses, beside articles that share them
    "what time do the front doors lock at night": "badge-access",
    "how far ahead must a flight be booked": "travel-booking",
    "when is the office closed for the year": "holiday-calendar",
    "what happens to a computer after three years": "laptop-refresh",
    "where can a bicycle be locked up": "bike-storage",
    "are meals at the autumn event paid for": "team-offsite",
}
COLLECTION = "topics"


class Overlap(FakeDecider):
    """The `--fake` decision model: a passage scores the share of the search's longer words its text holds."""

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        words = {w for w in re.findall(r"[a-z]+", state["search"].lower()) if len(w) > 3}
        self.answers = {}
        for passage in state["passages"]:
            held = set(re.findall(r"[a-z]+", f"{passage.get('article', '')} {passage['text']}".lower()))
            self.answers[passage["id"]] = len(words & held) / len(words) if words else 0.0
        return super().decide(state, questions)


def rank(workspace: Cerebro, question: str, right: str) -> tuple[int | None, list[str]]:
    """The right article's place in the search, from 1, or None when it is not there, and the reply's warnings."""
    result = workspace.lookup.query(YOU, "", matching=question)
    ids = [item.id for item in result.items]
    return (ids.index(right) + 1 if right in ids else None), result.warnings


def reciprocal(ranks: list[int | None]) -> float:
    return sum(1 / r for r in ranks if r) / len(ranks)


def main(argv: list[str]) -> int:
    fake = "--fake" in argv
    decider = Overlap() if fake else None
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "workspace"
        root.mkdir()
        (root / "settings.yaml").write_text(SETTINGS)
        workspace = Cerebro(root, trusted=True, extraction=FakeExtraction(), decider=decider)
        try:
            if workspace.lookup.rerank is None:
                print("FAIL\n- no re-ranker was built: " + "; ".join(n for n in workspace.notices if "rerank" in n))
                return 1
            for slug, (title, text) in ARTICLES.items():
                id = f"{COLLECTION}/{slug}"
                workspace.store.create(Identity(id, COLLECTION, title))
                workspace.store.write_compilation(Compilation(id, "Overview", text, by=f"{COLLECTION}/article"))
            reasons: list[str] = []
            before: list[int | None] = []
            after: list[int | None] = []
            reranker = workspace.lookup.rerank
            print(f"decision model: {'a scripted stand-in' if fake else workspace.models['decision'].model}\n")
            print(f"{'without':>7}  {'with':>4}  question -> right article")
            for question, slug in QUESTIONS.items():
                right = f"{COLLECTION}/{slug}"
                workspace.lookup.rerank = None
                plain, _ = rank(workspace, question, right)
                workspace.lookup.rerank = reranker
                again, notes = rank(workspace, question, right)
                before.append(plain)
                after.append(again)
                print(f"{plain or '-':>7}  {again or '-':>4}  {question} -> {right}")
                reasons += [f"{question!r} carried a note: {note}" for note in notes]
            print(f"\nmean reciprocal rank: {reciprocal(before):.2f} without re-ranking, {reciprocal(after):.2f} with")
            spent = workspace.store.total(LOG, "usd", {"layer": "rerank"})
            calls = len(decider.asked) if decider is not None else getattr(reranker, "calls", 0)
            print(f"re-ranking: {calls} decision calls, ${spent:.6f} recorded under rerank")
            if not calls:
                reasons.append("the decision model was never called")
            if reciprocal(after) < reciprocal(before):
                reasons.append("re-ranking put the right articles lower than the search did, on average")
            worse = [q for (q, _), b, a in zip(QUESTIONS.items(), before, after, strict=True) if (a or 99) > (b or 99)]
            better = sum(1 for b, a in zip(before, after, strict=True) if (a or 99) < (b or 99))
            print(f"re-ranking made {better} ranks better and {len(worse)} worse")
            reasons += [f"re-ranking put the right article lower for {q!r}" for q in worse]
        finally:
            workspace.close()
    if reasons:
        print("FAIL\n" + "\n".join(f"- {r}" for r in reasons))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
