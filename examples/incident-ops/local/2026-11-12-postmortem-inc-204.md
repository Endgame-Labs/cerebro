Service: Webhook Dispatcher
Service: Checkout Worker
Service: Ledger API
Incident: INC-204: Redis Failover Storm
Date: 2026-11-12

# Postmortem: INC-204 Redis Failover Storm

Severity: SEV-1. Duration: 68 minutes (2026-11-12 14:12 to 15:20 UTC). Incident commander: Priya Subramanian.

## What happened

An automated kernel patch rebooted `redis-east-2` at 14:12 UTC. Redis Sentinel ran with a quorum of 2 across only two availability zones, causing a quorum split that promoted a lagging replica (`redis-east-3`, 48 seconds behind primary).

## Root cause

Two faults compounded during failover. First, the 2-AZ Sentinel topology with quorum 2 allowed a split-brain promotion of lagging replica `redis-east-3`. Second, Webhook Dispatcher's 600 retry workers reconnected simultaneously on fixed 10-second intervals with no jitter, creating a thundering herd that hit the Redis `maxclients=4000` limit and triggered `WebhookQueueSaturation` (SEV-1).

## Service impact

- Webhook Dispatcher: error rate peaked at 14.2% with 28.5x error-budget burn and 4,820 ms p99 latency. Preliminary log counts estimated 18,400 dropped merchant webhooks, pending full ledger reconciliation.
- Checkout Worker: rate-limiter lookups against `redis-east` stalled, pushing p99 latency to 1,920 ms for 22 minutes and firing `CheckoutWorkerLatencyHigh`.
- Ledger API: tripped `LEDGER_READ_ONLY_FALLBACK=true` automatically at 14:18 UTC (`LedgerFallbackEngaged`, SEV-3), limiting p99 latency to 310 ms for 8 minutes with no lost postings.

## Immediate mitigation

Priya Subramanian failed traffic back to `redis-east-1` and throttled Webhook Dispatcher retry concurrency from 600 workers down to 150 workers.

## Open disagreement on follow-up

Tomasz Kowalski proposed raising Redis `maxclients` from 4,000 to 10,000 immediately to prevent connection exhaustion during failovers. Priya Subramanian opposed raising `maxclients`, warning that 10,000 connections on the 32 GB Redis nodes would trigger kernel OOM kills; she argued instead for keeping `maxclients=4000`, shipping exponential backoff with full jitter in Webhook Dispatcher, and moving Redis Sentinel to a 3-AZ quorum of 3.

## Action items

- OPS-515 (owner: Priya Subramanian, initial target: 2026-11-20): Migrate Redis Sentinel to a 3-AZ topology with quorum 3.
- OPS-516 (owner: Tomasz Kowalski, target: 2026-11-16): Replace fixed 10-second webhook retries with exponential backoff and full jitter.
