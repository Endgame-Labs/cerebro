---
evidence:
  - facts: { window: last_365_days }
  - query: { from: facts, window: last_365_days, limit: 150 }
  - script: { run: scripts/reproduce.py }
heartbeat: [change]
---

# Instructions

Explain only the shipped behavior of the named component in 60 to 90 words.
Lead with what it does in 0.3.2 and give one relevant input/output example.
For the parser, say what numeric representation and syntax it accepts. For
folding, say which operation it evaluates. For the printer, distinguish the
internal value from its spelling. Use executable observations, clearly labelled
as a numeric subset. Leave the investigation, release history, and pending work
to the other parts. Every sentence cites evidence.
