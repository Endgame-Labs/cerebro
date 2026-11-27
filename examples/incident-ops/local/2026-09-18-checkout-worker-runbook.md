Service: Checkout Worker
Date: 2026-09-18

# Runbook update: Checkout Worker

Updated by Maya Chen.

Checkout Worker processes merchant card-capture authorizations and runs idempotency lookups against `pg-read-replica-pool` before committing charges. It also reads rate-limit counters from `redis-east`.

## SLO targets and thresholds

- Availability SLO: 99.95% over a rolling 30-day window (21.6 minutes of error budget).
- Latency SLO: p99 under 250 ms; normal steady-state p99 is around 140 ms.
- Alert `CheckoutWorkerCrashLoop` pages SEV-2 when more than 4 pod restarts occur within 15 minutes.
- Alert `CheckoutWorkerLatencyHigh` pages SEV-2 when p99 exceeds 1,000 ms for 5 minutes.

## Escalation

Primary on-call is Platform SRE. Secondary service owner is Devon Okafor (Checkout and Payments).
