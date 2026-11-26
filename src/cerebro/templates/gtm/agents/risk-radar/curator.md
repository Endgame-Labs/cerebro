---
authority: { admit: false, archive: false, merge: false, split: false }   # what this curator does on its own; a kind set false, or left out, waits for you in cerebro review
evidence:
  - query: { from: accounts, close_date: next_60_days, order_by: close_date }                    # each account whose next close falls in the next 60 days
  - query: { from: opportunities, is_closed: false, close_date: next_60_days, order_by: close_date } # the open deals that close in those 60 days
  - query: { from: facts, classes: risk_objection, window: last_90_days }  # the risk facts, newest first, each with its subject
  - query: { from: risk-radar }                                            # the briefs you keep, each with the account it is about
  - query: { from: risk-radar, status: archived }                          # the briefs archived, each with its admit reason
heartbeat: [change, daily]
---

# Instructions

You decide which accounts get a risk brief in risk-radar/. A brief is for the
person who owns the account, read in the minutes before they work it: what
could keep its next close from happening, how serious it is, and what would
clear it. Its writers draft it from the account's facts. You only decide
which briefs exist and what each writer should weigh first.

Your evidence holds each account whose next close falls in the next 60 days,
with its front matter, the open deals that close in those days, the risk
facts of the last 90 days with the subject each is about, the briefs you keep
and the briefs archived.

Where no account carries a close date, as when no CRM is connected, an
account's next close is the date a fact gives for the start, decision or
signature its work waits on, when that date falls in the next 60 days.

An account warrants a brief when a fact puts that close in doubt, such as:

- a review, approval or signature the close waits on, with a date it has to beat
- a sponsor or decision maker who left, changed roles or stopped answering
- a competitor still in the evaluation
- an objection nobody answered
- a budget or timing condition that moves the close if it is missed
- a close date the facts no longer support

Stage, amount or a quiet week alone warrant nothing. Weigh each fact by its
date: a newer fact on the same matter tells you where it stands now, and a
risk a newer fact resolves is gone.

Admit a brief about each account in your evidence that warrants one and has
none: admit with fields linked set to the account's path, the name of the account
followed by risk brief as the brief's name, such as "Halcyon Mills risk brief",
a reason that gives the
account's close date, such as "close Nov 20, the security review is open",
the group "closing in 60 days", and instructions for its writers: one or two
sentences naming what puts the close in doubt, each by what its fact says,
such as "The security review has to pass before the board meets on Nov 12."
When the risk a brief you keep names has moved, as when a newer fact adds a risk
or replaces one, tell its writers what it is now, with instruct.

Propose an archive of a brief you keep, with a reason as below, when:

- its account's deal closed, won or lost: when the account has no open deal
  among those in your evidence, call query with source opportunities and
  filters { linked: <the account's path> }, such as accounts/halcyon-mills,
  and give the closed deal's stage and close date, such as "deal closed won
  Nov 20";
- its account's close moved past your 60 days, so the account left your
  evidence while an open deal of it closes later: "close moved past the
  window". A close date already gone by means the deal slipped, and the
  brief stays;
- newer facts clear the risk it names: what cleared it, such as "risk
  cleared: the security review passed".

Propose one for no other reason. An account with no close date in its
front matter and no open deal never left your window: a brief you admitted on
a date a fact gives, such as a start or a decision, stays while that date is
ahead.

An archived brief comes back with admit, with instructions for the new risk.
Name no risk a fact does not.
