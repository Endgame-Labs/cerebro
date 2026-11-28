---
evidence:
  - query: { from: services, slo_status: healthy }
  - left: { from: services, slo_status: healthy }
  - query: { from: services, slo_status: breached }
  - query: { from: services }
  - query: { from: incidents }
  - sql: { source: slo, query: "select service, target_availability_pct, budget_consumed_pct, p99_target_ms, status, updated_at from slo_budgets order by budget_consumed_pct desc" }
  - script: { run: scripts/flap_detector.py }
  - articles: [services, incidents]
    text_only: true
heartbeat: [weekly]
---

# Instructions

Write the Weekly Reliability Brief for Kestrel Pay's Platform SRE team to read
at the Monday reliability review. Keep it tight, scannable, and strictly cited.

Write five parts, each under its heading, in this exact order:

1. **SLO Budget Status**: a markdown table of every production service with
   columns `Service`, `SLO status`, `On-call risk`, `Budget consumed`, and
   `Availability target`. Link each service in the `Service` column (such as
   `[Webhook Dispatcher](../services/webhook-dispatcher.md)`). Copy every cell
   value (`slo_status`, `oncall_risk`, `budget_consumed_pct` with `%`, and
   `availability_target_pct` with `%`) directly from each service article's
   front matter. Each row cites the service through its markdown link and
   carries no footnote. Put nothing else in this part.
2. **Slipped Out of Budget**: list any service under the `Left` evidence that
   slipped out of `healthy` SLO status since the last weekly brief, linking its
   article and stating its new `slo_status` and `budget_consumed_pct`. When the
   `Left` evidence holds no service yet, write only:
   "The evidence holds no service that slipped out of healthy SLO status since
   the last weekly brief."
3. **Recurring Flap Signatures**: summarize any recurring time-of-week alert
   and latency patterns flagged by `flap_detector.py`, tying them to the
   documented root cause and open ticket cited in the service articles.
4. **Recent Incidents**: summarize each production incident in one bullet,
   linking its article (such as
   `[INC-204: Redis Failover Storm](../incidents/inc-204-redis-failover-storm.md)`),
   including severity, date, and verified impact counts (noting any audit
   corrections). Cite every exact fact ID you copy from the loaded articles.
5. **Priority On-Call Actions**: list the highest-priority open tickets with
   owner, current target date, and why each matters, citing only fact IDs that
   appear in your evidence.

Every prose sentence must cite its source (`[^fact_...]`, `[^sql:slo:N]`,
`[^script:scripts/flap_detector.py:N]`, or a linked article). Never compute
totals, sums, or averages of your own, and never invent a fact ID that is not
verbatim in your evidence.

# Output

## SLO Budget Status

## Slipped Out of Budget

## Recurring Flap Signatures

## Recent Incidents

## Priority On-Call Actions
