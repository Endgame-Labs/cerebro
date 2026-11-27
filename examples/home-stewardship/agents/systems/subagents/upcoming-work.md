---
evidence:
  - facts: { classes: [contractor_quote, seasonal_task], window: last_365_days }
  - facts: { window: last_365_days }
  - due_tasks: { days: 90, system: this }
  - query: { from: contractors }
heartbeat: [change, manual]
---

# Instructions

Write the upcoming scheduled maintenance, open contractor quotes, and deferred
future work for this system.

When a scheduled date moved or a quote was revised and split into phases, state
the current scheduled date, scope, and dollar amount first, and note the earlier
date or quote it replaced, citing both. Include any weather constraints,
pre-visit steps, or logged entries from `due_tasks`. Do not repeat standing
specifications, radiator bleed sequences, or paint codes that Specs and quirks
already carries; mention a quirk only in one brief phrase when a scheduled
visit directly depends on it.

Keep the system's front matter current with `override`:
- Set `next_service_due` to the soonest upcoming scheduled service or
  maintenance due date for this system, formatted as `YYYY-MM-DD`. When a date
  was moved or logged in `due_tasks`, use the active date rather than the
  superseded one.
- Set `linked` to a list of the contractor article paths from the `contractors`
  query that service or quoted this system, such as
  `["contractors/alder-and-sons-plumbing"]`.

Every sentence cites every fact or `due_tasks` record it draws on. Write short,
direct sentences rather than packing several dates, scopes, and dollar figures
into one sentence, and give each scheduled phase or milestone a sentence of its
own. Leave out anything you cannot cite.

# Output

## Upcoming work
