---
model: { provider: fireworks, model: glm-5p3, effort: high }
evidence:
  - query: { from: scenes, readiness: "needs rehearsal", order_by: next_rehearsal }
  - query: { from: cues, readiness: "needs rehearsal", order_by: next_rehearsal }
  - query: { from: props, readiness: "needs rehearsal", order_by: next_rehearsal }
  - articles: [scenes, cues, props]
heartbeat: [change, daily]
---

# Instructions

Write a daily call briefing of at most 250 words for Mara Finch and substitute crew.
Group related work, and say each task once. Use the Oxford comma, and plain punctuation.
Lead with the next rehearsal's date, time, and the work still needed from the queried
readiness and next_rehearsal fields. Explain the picnic's current timing, the silent clock
check, and any cue label that could mislead a substitute. Draw details and citations from
the articles. Link each item to its article using paths relative to articles/briefings/, such as
[Q-L02](../cues/q-l02.md), [The midnight picnic](../scenes/the-midnight-picnic.md),
and [Auntie](../props/the-clock-that-must-never-ring.md). Every sentence cites the facts behind it.
Leave retired work out of the active call, but retain a cited warning where an old cue
could be called by mistake. Do not invent a task or turn an old date into a current call.
