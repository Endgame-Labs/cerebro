---
evidence:
  - facts: {}                                             # what the site reports, mail, and logs say of this subcontractor
  - query: { from: rfis, linked: this, order_by: due }    # the RFIs that link to this subcontractor, by due date
heartbeat: [change]
---

# Instructions

Write a briefing on one subcontractor for the project manager, who reads it
before a call with them. Lead with where their work stands today and what
changed most recently, then what they are waiting on and what is waiting on
them. Every sentence cites a fact.

Cover, where the facts give it:

- Their scope on this job and their lead contact.
- Progress against the schedule, with dates. When a date moved, give the old
  date and the new one, and why. When a later record corrects an earlier one,
  say what the correction was and state only the corrected version as true.
- Every RFI your query lists, open ones first, each by number, linked to its
  article such as [RFI-014](../rfis/rfi-014.md), with its state and due date
  as the RFI's fields give them. Say what work an open one holds up.
- Money: change orders by number, what was asked, what was approved or is
  pending, and any dispute over who pays.
- Safety events and quality findings on their work.

Write what happened on the job. A sentence that only says a message exists,
or who it was sent to, tells the reader nothing: leave it out. Say each
thing once, in the part where it matters most.

Where the project manager and the subcontractor disagree, give both sides as
the records state them, without taking one.

Set trade to their trade in two or three words, such as structural steel,
and contact to their lead contact's name, with override.

# Output

## Where they stand
## RFIs
## Money
## Watch
