---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - query: { from: factions }
  - query: { from: facts, window: last_365_days }
heartbeat: [change, daily]
---

# Instructions

You decide which Brineport locations get a location article.

Ask the `campaign` source with `fetch` for `header_values` with `word` set to `location` to see which sites recur across
the session recaps, handouts, and prep notes. Admit recurring campaign locations that appear in two or more records and
host active scenes or faction politics (such as Old Lantern Dock, The Salt Exchange, Brine Citadel, and Tidewatch
Lighthouse).

Leave out passing mentions of minor spots or off-stage places that never host a scene, such as Saltgate Market or High
Reef Quarry.

When admitting a location, set `fields` with `linked` listing the faction article id or ids from `factions` that
control, occupy, or actively contest that site (for example, `["factions/the-salt-guild"]`). Use `link` to add a
faction link if a faction article was admitted after the location.
