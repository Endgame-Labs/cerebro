Service: Ledger API
Date: 2026-09-25

# Runbook update: Ledger API

Updated by Nadia Benali.

Ledger API records double-entry settlement postings and serves merchant balance queries.

## SLO targets and thresholds

- Availability SLO: 99.99% over a rolling 30-day window (4.3 minutes of error budget).
- Latency SLO: p99 under 120 ms; normal steady-state p99 is around 74 ms.
- Read-only degradation threshold: when Redis lock latency exceeds 200 ms for 60 seconds, Ledger API automatically enables `LEDGER_READ_ONLY_FALLBACK=true`, keeping merchant balance reads healthy while buffering non-urgent settlement batches in Postgres.

## Planned maintenance

Nadia Benali scheduled a zero-downtime `settlement_entries` table partition migration for 2026-12-08. Pre-migration index preparation is tracked under OPS-510 (owner: Nadia Benali).
