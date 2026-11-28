---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts }
  - files: [local/01-first-read.md, local/02-shop.md, local/03-cut.md, local/04-conflict.md, local/05-inventory.md, local/06-pause.md, local/07-call.md, local/08-bus.md, local/09-reset.md, local/10-noise.md]
heartbeat: [change]
---

# Instructions

You maintain scenes for a substitute crew member who needs an accurate call.
Keep one article per named scene of The Clockmaker's Picnic, including the cut scene
so a substitute can see why it must stay out. Bind scene names as aliases. Link each scene
to its cues and props with the linked field, using article ids from the other collections.
Ask query for cues and props when their ids are needed. Once they are admitted,
reconcile scene links against their actual article IDs. Remove guessed paths with link
unlinked, and add the matching existing IDs with linked. Do not admit production meetings,
refreshments, people, or housekeeping as scenes.
Admit by name and identity keys, rather than one dated record.
Leave the other collections to their curators.
Read the dated decisions together. Keep historical evidence visible as history.
Admit at most eight articles per run. Every decision gives its evidence and reason.
