Service: Checkout Worker
Incident: INC-141: Read-Replica Lockup
Date: 2026-05-19

# Postmortem: INC-141 Read-Replica Lockup

Severity: SEV-2. Duration: 42 minutes (2026-05-19 11:05 to 11:47 UTC). Incident commander: Devon Okafor.

## What happened

An unbatched weekly ledger audit ran against the primary Postgres database during morning traffic and held table locks that stalled payment capture in Checkout Worker.

## Root cause

The ledger reconciliation sweep ran directly on the primary Postgres instance without statement timeouts or batch limits, blocking Checkout Worker write transactions.

## Temporary workaround left in place

To unblock primary traffic immediately, Devon Okafor moved the weekly reconciliation job (`reconcile-sweep`) to run as a cron every Tuesday at 03:00 UTC against `pg-read-replica-pool`, the shared read-replica pool that Checkout Worker queries for idempotency checks. That cron executes a `VACUUM` and full-table scan that saturates replica connections for about 45 minutes.

Because saturated replica connections had previously wedged Checkout Worker pods, Devon Okafor also set an interim pod watchdog `CHECKOUT_POOL_WATCHDOG_MS=1800` in Checkout Worker, which hard-restarts any worker pod with SIGTERM whenever database connection wait time exceeds 1,800 ms.

## Action items

- OPS-412 (owner: Devon Okafor, target: 2026-06-30): Move `reconcile-sweep` off `pg-read-replica-pool` onto an isolated analytics replica and remove the `CHECKOUT_POOL_WATCHDOG_MS=1800` watchdog. Note: later deprioritized in June 2026 during Q3 PCI audit work and left open.
