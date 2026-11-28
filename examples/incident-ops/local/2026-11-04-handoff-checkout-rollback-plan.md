Service: Checkout Worker
Date: 2026-11-04

# Shift handoff: Checkout Worker Go rollback plan

Author: Devon Okafor.

Tuesday 2026-11-03 at 03:10 UTC repeated the exact same pattern: `CheckoutWorkerCrashLoop` fired SEV-2 with 8 pod restarts and p99 latency at 2,340 ms (10.6x error-budget burn rate).

Devon Okafor agreed with Kenji Watanabe's Go 1.24.2 hypothesis and scheduled a rollback of Checkout Worker to Go 1.23.9 (`sha-f04b81d`) for 2026-11-06 to stop the Tuesday restarts.
