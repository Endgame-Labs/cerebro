---
evidence:
  - facts: { window: last_365_days }
  - rumors: { article: this }
  - query: { from: characters, linked: this }
  - articles: [this.linked]
heartbeat: [change]
---

# Instructions

Write a location briefing for the game master and players preparing for Session 14 of The Salt Crown.

Cover who currently controls or contests the location, its physical layout and hidden features, recent events and
pressing dates, and any unverified tavern or dockside rumors connected to this place. When a scheduled date changed in a
later order, state the current rescheduled date first and note the cancelled date briefly.

Use `override` to set `controlled_by` to the faction currently holding the location (or `contested` when two factions
struggle over it). Keep unverified rumors strictly under Dockside Rumors and label them as unverified hearsay, naming
who spoke them when the evidence says so.

Every sentence must cite a fact using the exact id from your evidence. Leave out anything you cannot cite.

# Output

## Overview

## Recent Events and Hazards

## Dockside Rumors
