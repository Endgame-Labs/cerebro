---
evidence:
  - query: { from: claims, verification_state: disputed }
  - query: { from: claims, verification_state: unresolved }
  - articles: [stories]
budget: { tokens_per_run: 20000, tool_calls: 8 }
heartbeat: [daily]
---

# Instructions

Write today's editorial call list in about 150 words from the reporting file,
whose dates remain historical. Read the claim articles returned by the
verification_state queries. Use each source's full name as a short heading,
then give the precise cited assertion or factual gap that motivates calling
that person. Preserve fact citations from the articles and distinguish an
unverified assertion from a disproved one. Keep each attributed sentence
intelligible on its own by naming the speaker in full. A call item proposes
reporting work, not an action the source has agreed to do. Do not invent a
phone number or a deadline.
