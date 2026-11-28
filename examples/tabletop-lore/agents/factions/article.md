---
evidence:
  - facts: { window: last_365_days }
  - query: { from: characters, linked: this }
  - query: { from: locations, linked: this }
heartbeat: [change]
---

# Instructions

Write a concise faction briefing for the game master and players heading into Session 14 of The Salt Crown.

Cover the faction's current standing with the party, its current treasury and how it changed, who leads it, which
locations it controls or contests, and the oaths, betrayals, and rivalries shaping its next move.

Use `override` to set `standing` and `treasury_gp` to the latest values the ledger or recent facts give, and set
`lead_npc` to the faction's leader when the facts name one. Under Current Standing, write plain cited sentences directly
from the ledger standing, treasury, and lead NPC facts, noting any earlier treasury figure briefly when cited.

Every sentence must cite a fact using the exact citation id from your evidence. Write plain prose without uncited bold
lead-in labels. Leave out anything you cannot cite.

# Output

## Current Standing

## Operations and Holdings

## Oaths, Betrayals, and Rivalries
