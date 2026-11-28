---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - held: {}
  - query: { from: facts, window: last_365_days }
heartbeat: [change, daily]
---

# Instructions

You decide which political factions in Brineport get a faction article.

Admit every faction listed in your held records from the faction ledger, admitting each by its held record so the
article carries its `faction_id` key and receives its standing, treasury, and lead NPC field facts. Name each faction
exactly as its ledger row names it: The Salt Guild, The Tidebreakers, Free Captains' Compact, and Tidewatch Priory.

Leave out civic bodies without a ledger row (such as the Fractured Quorum), market districts, individual ships, and the
workspace's own table (`Salt Crown Table`). Give every admit and leave_out a short, neutral reason naming the ledger
record or facts behind it.
