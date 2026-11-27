---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts, window: last_365_days }
  - query: { from: strains }
heartbeat: [change, daily]
---

# Instructions

Decide which bacterial strains belong in the `strains` collection. This
collection documents the lab's maintained glycerol stocks and strain-specific
handling limits (heat-shock duration, recovery temperature, antibiotic
concentration, and freezer box location).

Start each run by asking the `local` source with `fetch` for `header_values`
with `word` set to `strain` (and `notes` with `each` set to `strain` and `with`
set to `protocol`). Also check your `facts` evidence when facts are present.

Admit every bacterial strain that appears on `Strain:` header lines in the lab's
notebook files or is shown in facts to be maintained in the lab's freezer stocks
and used across bench runs (such as `E. coli K-12 Delta-Lon` and
`E. coli DH5alpha-T1R`). Give clear aliases in `keys` when notebook entries
shorten the strain name (such as `Delta-Lon` for `E. coli K-12 Delta-Lon`, and
`DH5alpha-T1R` and `DH5a-T1R` for `E. coli DH5alpha-T1R`) so every run note
binds to the right strain article.

Leave out strains mentioned only in passing as an outside comparison (such as
`BL21`), plasmids, reagent lots, protocols, and facilities or seminar notices.
Give each proposal or left-out candidate a concise, neutral one-line reason.
Admit at most 15 strains in one run.
