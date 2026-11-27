---
authority: { admit: true, archive: true, merge: false, split: false }   # the RFI log is the record: admit and archive on its own
evidence:
  - held: {}                               # the rows of the RFI log no article holds yet, newest first
  - query: { from: rfis, state: void }     # the RFIs their writer found void or duplicated
heartbeat: [change, daily]
---

# Instructions

Keep one article for each live RFI in the log. Admit each held row by its
record, under the group "RFI log", with the reason "an RFI in the log". A row
whose name says it is void or a duplicate gets no article: leave it out with
that reason. Admit nothing that is not a row of the log, whatever else a run
asks to see first.

Archive each RFI your query lists as void, with the reason its facts give,
such as a duplicate of another RFI. The log keeps the row. The wiki needs no
article for it.
