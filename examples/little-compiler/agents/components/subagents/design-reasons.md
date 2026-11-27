---
evidence:
  - files: [local/2026-09-03-numeric-contract.md, local/2026-09-09-folding-contract.md, local/2026-09-18-print-contract.md]
  - facts: { window: last_365_days }
  - query: { from: facts, window: last_365_days, limit: 150 }
heartbeat: [change]
---

# Instructions

Explain only why the named component behaves that way, in 60 to 90 words.
Name the decision, the concrete user need it serves, and the tradeoff or rejected alternative. Read the original design note for the reason behind the rule.
Do not repeat shipped examples, incident chronology, or pending work. Explain
the reason in plain words, rather than listing tickets. Every sentence cites its evidence. Keep to this component even when related facts name the whole pipeline.
