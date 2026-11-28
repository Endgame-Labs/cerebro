Service: Webhook Dispatcher
Date: 2026-10-08

# Runbook update: Webhook Dispatcher

Updated by Tomasz Kowalski.

Webhook Dispatcher signs and delivers payment-state webhooks to merchant HTTPS endpoints from a Redis-backed retry queue on `redis-east`.

## SLO targets and baseline settings

- Availability SLO: 99.90% over a rolling 30-day window (43.2 minutes of error budget).
- Delivery latency SLO: p99 under 500 ms; normal steady-state p99 is around 215 ms.
- Outbound HTTP timeout: 15 seconds per merchant request (global setting across all 600 retry workers).
- Retry policy: fixed 10-second retry intervals with no jitter.

## Escalation

Primary on-call is Platform SRE. Service owner is Tomasz Kowalski (Webhook Platform).
