---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts, window: last_365_days }
heartbeat: [change, weekly]
---

# Instructions

Decide which outdoor zones and garden systems belong in the `garden` collection
for the half-acre lot at 418 Cedar Hill Road.

Call `fetch` on `local` for `header_values` with `word` set to `garden` and read
the facts in your evidence. Admit each established garden area or irrigation
system (such as the South Slope Orchard or the Rainwater Cistern and North
Beds), giving it a `garden` key matching its `Garden:` header line so seasonal
notes bind to it.

Leave out indoor house mechanical systems (those belong in `systems`),
contractors (`contractors`), routine errands, and irrelevant referrals. Record
excluded candidates with `leave_out` and a one-line reason. Admit at most 10
garden articles per run.
