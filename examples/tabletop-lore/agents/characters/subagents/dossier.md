---
evidence:
  - facts: { window: last_365_days }
  - articles: [this.linked]
heartbeat: [change]
---

# Instructions

Write the confirmed canon dossier for this character heading into Session 14.

State who the character is, their faction allegiance, where they currently are, and the confirmed oaths, betrayals,
arrests, or public actions tied to them. When a later fact corrects an earlier accusation or moves a scheduled date,
state the latest confirmed truth clearly and cite both facts if you mention what changed.

Use `override` to set two front-matter fields from the latest facts:
- `status_in_world`: one of `alive`, `missing`, `imprisoned`, or `dead`. Use `imprisoned` if the character is locked in
  a prison, `missing` if they vanished and are publicly sought or unaccounted for, `dead` if killed, and `alive`
  otherwise.
- `allegiance`: the name of their primary faction (for example, `The Salt Guild`, `The Tidebreakers`,
  `Free Captains' Compact`, or `Tidewatch Priory`), or `unaligned` when no faction claims them.

Every sentence must cite a fact using the exact id from your evidence. Leave out unverified tavern rumors and speculative
secret-identity clues; the whispers part covers those.

# Output

## Dossier
