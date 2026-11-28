---
evidence:
  - articles: [characters, factions, locations]
  - query: { from: facts, classes: oath, window: last_120_days }
  - query: { from: facts, classes: betrayal, window: last_120_days }
  - query: { from: facts, classes: item_transfer, window: last_120_days }
  - rumors: {}
heartbeat: [change]
---

# Instructions

Write "Previously On — Session 14 Brief", a table recap for the game master and players opening Session 14 of The Salt
Crown.

Keep each part tight and playable at the table:
- **Previously On:** what shifted in the latest sessions around Old Lantern Dock, The Salt Exchange, and Brine Citadel.
- **Missing and Imprisoned:** who is imprisoned, who is publicly missing (and any secret sanctuary known to the GM), and
  active bounties.
- **Oaths, Betrayals, and Relics:** broken oaths, active vows, and who currently holds key items such as the Obsidian
  Tide-Glass and the Brass Harbour Key.
- **Tavern Rumors:** unverified whispers logged from dockside and tavern speakers, clearly attributed as hearsay without
  declaring unconfirmed secret identities as proven fact.
- **Pressing Clocks for Session 14:** upcoming deadlines and dates in chronological order, using the latest amended date
  whenever an earlier date was cancelled or rescheduled.

Keep the exact citation ids from your evidence on every sentence, such as `[^fact_...]`. Leave out anything you cannot
cite.

# Output

## Previously On

## Missing and Imprisoned

## Oaths, Betrayals, and Relics

## Tavern Rumors

## Pressing Clocks for Session 14
