---
evidence:
  - facts: { classes: [specification, quirk, warranty, field_value], window: last_365_days }
  - facts: { window: last_365_days }
heartbeat: [change, manual]
---

# Instructions

Write the specifications, warranties, and physical quirks for this house system
so a homeowner or visiting contractor can work on it safely years from now.

Include exact model and serial numbers, install dates, locations, capacities,
breaker or valve numbers, bleed-valve sequences, paint codes, hardware part
numbers, and warranty expiration dates or conditions that keep coverage valid.
Put every physical quirk, hidden shutoff, or do-not-touch warning in plain,
unmistakable wording with the exact component label and what goes wrong if
ignored.

When a later fact corrects an earlier inspection report or technician
assumption (such as active knob-and-tube wiring found after an inspection said
none remained, or a revised pressure setting), state what holds today first and
note what the earlier record got wrong, citing both.

Keep the system's front matter current from its facts using `override`: set
`model`, `installed_on` (YYYY-MM-DD), and `warranty_until` (YYYY-MM-DD) when the
facts state them. Leave out any field no fact gives.

Do not list past invoice logs or future scheduled quotes here; other parts own
those. Every sentence cites a fact. Leave out anything you cannot cite.

# Output

## Specs and quirks
