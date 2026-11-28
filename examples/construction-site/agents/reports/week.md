---
evidence:
  - query: { from: rfis, state: open, due: last_30_days, order_by: due }   # open RFIs whose answer is past due
  - query: { from: rfis, state: open, due: next_14_days, order_by: due }   # open RFIs due in the next two weeks
  - query: { from: rfis, answered: last_7_days, order_by: answered }       # RFIs answered in the last week
  - query: { from: facts, window: last_7_days }                            # what the reports, mail, and logs said this week
heartbeat: [weekly]
---

# Instructions

Write the project manager's Monday briefing on the Larkspur Branch Library,
for someone with five minutes before the owner's meeting. The heading of
each query names the day it was read, which is today.

Start with the two or three things that most need the project manager this
week, each in one sentence, soonest consequence first.

Then the RFIs, in three parts, each a heading: Past due, Due in the next two
weeks, and Answered this week. Each part is a table of the RFIs its query
returned, soonest due date first, with the columns RFI, Subcontractor, Due,
Answered, and State. The RFI cell links to its article, such as
[RFI-014](../rfis/rfi-014.md), and each other cell copies the value the RFI's
fields give, empty where a field is missing. The row cites the RFI by its
link and carries no footnote. Under a part whose query returned no RFI,
write one line that says the evidence holds none. Under a table, a sentence
may say what an RFI holds up on site, citing the fact that says so.

Then the week on site: progress, dates that moved, money that moved, and
anything the owner should hear from us before hearing it elsewhere. Say each
thing once, in the part where it matters most. Every sentence cites a fact.
