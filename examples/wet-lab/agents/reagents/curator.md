---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - held: {}
  - query: { from: facts, window: last_365_days }
  - query: { from: reagents }
heartbeat: [change, daily]
---

# Instructions

Decide which reagent lots belong in the `reagents` collection. This collection
tracks every inventoried reagent lot from the reagent lot ledger so bench
scientists and onboarding students can check storage temperatures, active
quarantines, and lot-specific failure modes before pipetting.

Admit every reagent lot that appears in your held records from the `ledger`
source, by its held record, using the exact reagent name the record gives (such
as `Buffer X — Lot 4B` or `LbCas12a Enzyme Stock — Lot E20`). Include useful
aliases in `keys` so notebook facts link cleanly to the article, such as a
shortened form without the em dash (`Buffer X Lot 4B`, `LbCas12a Lot E20`,
`2x Gibson Master Mix Lot G11A`, `Kanamycin Lot K09`) alongside the record's own
`reagent_id` and `lot_number` keys. Set `fields` from the record when admitting
if known, leaving unknown fields for the writer.

Do not admit unlogged common consumables (such as LB agar, SOC media,
nuclease-free water, or lint-free wipes), depleted historical lots that have no
row in the reagent ledger (such as `Buffer X — Lot 3A`), bacterial strains, or
protocols. Leave out any candidate that is not an inventoried reagent lot with
a clear one-line reason.

Keep every reason plain, factual, and in a neutral voice. Admit at most 20
reagent lots in one run.
