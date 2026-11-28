---
evidence:
  - facts: { window: last_365_days }
  - query: { from: facts, window: last_365_days, limit: 150 }
heartbeat: [change]
---

# Instructions

Give this component's failure status in 60 to 90 words. Name the corrected
root cause and whether the repair shipped. If work remains, give its owner,
next step, and date when supported. A draft fix remains a proposal, and a
review date is not a release date. If an investigation cleared this component,
say that briefly. Do not repeat the behavior demonstration or design tradeoff.
Avoid inventories of closed tickets. Every sentence cites a fact.
