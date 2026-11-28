---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts, window: last_365_days }
  - held: {}
heartbeat: [change, daily]
---

# Instructions

Decide which production services belong in the `services` collection.

First, inspect the held records from `ops-log` and call `fetch` on `local` with
question `notes` (`each: service`, `with: incident`). Admit every production
service that appears in deployment/alert logs or in runbooks, postmortems, or
shift handoffs.

When admitting a service that appears in `ops-log` held records, include its
`service_slug` key (for example `keys: {"service_slug": ["checkout-worker"]}`)
and its display name (for example `Checkout Worker`, `Webhook Dispatcher`,
`Ledger API`) so that structured deployment and alert rows bind to the service
article.

Leave out anything that is not a production Kestrel Pay service: staging-only
test harnesses (such as `sandbox-echo`), external merchants (such as Northstar
Retail), internal infrastructure hostnames (such as `redis-east-2` or
`pg-read-replica-pool`), team lunches, and corporate IT notices. Give a short
one-line reason for every admit and every leave_out.
