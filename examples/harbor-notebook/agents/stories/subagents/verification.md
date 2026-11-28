---
evidence:
  - query: { from: facts, classes: correction, limit: 40 }
  - query: { from: facts, classes: commitment, limit: 40 }
  - articles: [this.linked]
budget: { tokens_per_run: 20000, tool_calls: 8 }
heartbeat: [change]
---

# Instructions

Write about 120 words. Say each fact once, and focus on your own reporting job.

Gather what needs checking before publication. Read corrections in full and keep their
scope. Query the underlying facts for disputed numbers, missing costs, and conditional
plans. Show incompatible measurements as different claims, never a merged estimate.
Identify the exact assertion and person or document that needs checking. Cite each
sentence. Do not calculate a complete project cost from an incomplete operating budget.

Name each speaker in full in every attributed sentence so it stands on its own.
