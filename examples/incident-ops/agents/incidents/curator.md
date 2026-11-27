---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - query: { from: services }
  - query: { from: facts, window: last_365_days }
heartbeat: [change, daily]
---

# Instructions

Decide which production incidents belong in the `incidents` collection.

Call `fetch` on `local` with question `notes` (`each: incident`, `with: service`)
to list every incident named in postmortems and notes, and cross-check against
the facts. Admit every real production incident (for example `INC-204: Redis
Failover Storm`, `INC-211: Webhook Timeout Cascade`, `INC-141: Read-Replica
Lockup`).

When admitting an incident, set `fields.linked` to the list of affected service
article IDs from the `services` query (for example
`["services/webhook-dispatcher", "services/checkout-worker", "services/ledger-api"]`),
and include any short incident code (such as `INC-204`) in `keys` under `alias`
so facts mentioning the incident code alone still link cleanly.

Leave out staging chaos drills (such as `sandbox-echo`), team lunches,
corporate IT VPN notices, and routine shift handoffs that do not represent a
separate production incident. Give a one-line reason for each admit and
leave_out.
