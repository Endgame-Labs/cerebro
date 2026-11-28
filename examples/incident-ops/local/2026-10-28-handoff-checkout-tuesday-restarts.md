Service: Checkout Worker
Date: 2026-10-28

# Shift handoff: Checkout Worker Tuesday restarts

Author: Kenji Watanabe (night shift SRE).

At 03:11 UTC on Tuesday 2026-10-27, `CheckoutWorkerCrashLoop` paged SEV-2 after 7 Checkout Worker pods restarted within 12 minutes and p99 latency climbed past 2,200 ms. The spike cleared by 03:50 UTC without manual intervention.

Checkout Worker deployed Go 1.24.2 (`sha-a91c4e2`) on 2026-10-22. Kenji Watanabe suspected a Go 1.24.2 garbage-collection regression during overnight low-traffic heap compaction and recommended watching next Tuesday at 03:00 UTC before deciding on a rollback.
