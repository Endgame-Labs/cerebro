---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - query: { from: claims }
  - query: { from: facts, limit: 150 }
budget: { tokens_per_run: 30000, tool_calls: 16 }
heartbeat: [change]
---

# Instructions

Fetch local notes with each project and with source to find the reporting subject
before extraction. The returned project header is an article key, not a claim of truth.

Propose one editor briefing named Who would the night ferry serve? with keys including
Eastbank night ferry. Query the claims collection and link all substantive ferry claim
articles to the story by their actual ids. Refresh those links when the claims exist.
This is one reporting question, not one article per record. Propose nothing for kitchen notices.

When claim articles do not exist yet, propose the story without links and finish.
When they appear, link the existing story rather than proposing it again.
