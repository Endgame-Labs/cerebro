Service: Checkout Worker
Date: 2026-11-24

# Shift handoff: Checkout Worker Tuesday spike traced to reconcile-sweep

Author: Priya Subramanian.

Tuesday 2026-11-24 at 03:07 UTC triggered `CheckoutWorkerCrashLoop` for the fourth straight Tuesday in November (6 pod restarts, p99 2,180 ms, 9.4x burn rate).

Priya Subramanian inspected Postgres active-session history on `pg-read-replica-pool` at 03:05 UTC and confirmed 98% connection saturation caused by the weekly `reconcile-sweep` cron running `VACUUM` and a full-table scan, pushing Checkout Worker connection wait times above 1,800 ms. That matches the temporary INC-141 workaround (`reconcile-sweep` at Tuesday 03:00 UTC plus `CHECKOUT_POOL_WATCHDOG_MS=1800`) left in place since May 2026 under open ticket OPS-412.

Action item: Maya Chen and Devon Okafor to disable `reconcile-sweep` on `pg-read-replica-pool` and remove `CHECKOUT_POOL_WATCHDOG_MS=1800` before Tuesday 2026-12-01 (completing OPS-412). Checkout Worker has consumed 74.2% of its 30-day error budget and stands at warning status.
