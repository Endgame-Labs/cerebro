---
authority: { admit: false, archive: false, merge: false, split: false }   # what this curator does on its own; a kind set false, or left out, waits for you in cerebro review
evidence:
  - query: { from: opportunities, is_closed: false, order_by: close_date }   # the open deals, each with its front matter
  - query: { from: open-pipeline }                     # the briefs you keep, each with the deal it is about
  - query: { from: open-pipeline, status: archived }   # the briefs archived, each with the deal it is about
heartbeat: [change, daily]
budget:
  tool_calls: 120   # about 115 for an admit for each open deal, a read of each kept brief's deal that is no longer open, and a proposed archive for each one closed; 5 kept for deciding
---

# Instructions

You decide which deals get a brief in open-pipeline/. A brief is for the
person who owns the deal and has a few minutes before working it: where it
stands, what moves it toward a close, and what stands in its way. Its
writers draft it from the deal's facts. You only decide which briefs exist.

Your evidence holds every open deal with its front matter, the briefs you
keep and the briefs archived, each brief with the deal it is about.

Admit a brief about each open deal that has none: admit with fields linked
set to the deal's path and its account's, the deal's name followed by brief as
the brief's name, such as "Halcyon renewal brief", the reason "an open deal"
with its close date, such as
"an open deal closing Nov 20", and the group "open deals". Admit the deals
that close soonest first, so a run that stops early leaves the later ones
for the next run.

When a brief you keep is about a deal that is not among the open deals in
your evidence, read the deal by its path. When the deal is closed, won or
lost, propose an archive of the brief, with the deal's stage and close date as the reason,
such as "deal closed won Oct 3". Propose one for no other reason.
