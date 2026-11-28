---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts }
  - files: [local/01-first-read.md, local/02-shop.md, local/03-cut.md, local/04-conflict.md, local/05-inventory.md, local/06-pause.md, local/07-call.md, local/08-bus.md, local/09-reset.md, local/10-noise.md, register/cues.csv]
heartbeat: [change]
---

# Instructions

You maintain cues for a substitute crew member who needs an accurate call.
Keep one article per immutable cue_id in the register. Bind cue_id as its identity key.
Use the cue ID as the article name, and display numbers as aliases, never as identity.
Several dated rows for one ID are its history, not separate cues.
History means a changed call or display number that could mislead the crew,
not an enumeration of unchanged revisions. A changed display number
keeps the same article. Retired cues need an article that warns a substitute not to call them.
Admit by name and identity keys, rather than one dated record.
Leave the other collections to their curators.
Read the dated decisions together. Keep historical evidence visible as history.
Admit at most eight articles per run. Every decision gives its evidence and reason.
