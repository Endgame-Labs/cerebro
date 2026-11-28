---
evidence:
  - facts: { window: last_365_days }
  - due_tasks: { days: 90, system: this }
heartbeat: [change, manual]
---

# Instructions

Write the stewardship manual for this garden area at 418 Cedar Hill Road, for
the homeowners and future owners.

Under Inventory and quirks, record every plant cultivar and rootstock or
equipment model, serial number, storage location, warranty date, soil test
result, and physical quirk (such as buried irrigation valves or seasonal
clogging hazards).

Under Seasonal care and upcoming work, record standing seasonal rules (such as
pruning windows and what never to do in wet weather), upcoming scheduled tasks,
any dates that moved due to weather, and any `due_tasks` maintenance entries.
When a scheduled date moved, lead with the current date and note the original
date it replaced, citing both.

With `override`, set `next_service_due` to the soonest upcoming scheduled task
or maintenance due date as `YYYY-MM-DD`. Every sentence cites a fact or
`due_tasks` record.

# Output

## Inventory and quirks

## Seasonal care and upcoming work
