Service: Checkout Worker
Date: 2026-11-17

# Shift handoff: Go rollback did not stop Tuesday Checkout Worker restarts

Author: Kenji Watanabe.

Checkout Worker rolled back from Go 1.24.2 to Go 1.23.9 (`sha-f04b81d`) on 2026-11-06 and shipped `sha-c31e09b` on 2026-11-16, yet `CheckoutWorkerCrashLoop` fired again on Tuesday 2026-11-10 (6 restarts, p99 2,290 ms) and Tuesday 2026-11-17 at 03:09 UTC (9 restarts, p99 2,480 ms, 11.8x burn rate).

This disproves the Go 1.24.2 garbage-collection hypothesis. Pod exit logs on 2026-11-17 show `context deadline exceeded waiting for pg-read-replica-pool connection` immediately before each pod receives SIGTERM and restarts.
