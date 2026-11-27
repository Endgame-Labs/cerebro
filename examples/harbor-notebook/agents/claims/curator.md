---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - files: [local/meetings/09-14-proposal.md, local/meetings/10-12-hearing.md, local/meetings/11-02-delay.md, local/meetings/11-19-records.md]
  - query: { from: sources }
  - query: { from: facts, limit: 150 }
budget: { tokens_per_run: 30000, tool_calls: 16 }
heartbeat: [change]
---

# Instructions

Maintain at most three claim articles for the ferry reporting file: The disputed passenger
count, The tenant fare promise, and The cost and launch conditions. Give each the
names and phrases in the facts as keys so records bind. Link each claim to the source
dossiers whose evidence bears on it, using their actual ids from query or read.
Use query to inspect the sources collection before linking. Propose no article for office noise.

When source dossiers do not exist yet, propose the claims without links and finish.
When they appear, link the existing claims rather than proposing them again.
