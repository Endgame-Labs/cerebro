---
evidence:
  - facts: { window: last_365_days }
  - sql: { source: slo, query: "select service, target_availability_pct, budget_consumed_pct, p99_target_ms, status, updated_at from slo_budgets order by budget_consumed_pct desc" }
  - sql: { source: slo, query: "select service, ts_utc, day_of_week, hour_utc, p99_ms, error_rate_pct, burn_rate, restarts from slo_hourly where burn_rate >= 2.0 order by ts_utc desc" }
  - script: { run: scripts/flap_detector.py }
heartbeat: [change, manual]
---

# Instructions

Write the Reliability part for this service alone. Ignore rows in the SQL and
script evidence that belong to other services, and ignore facts from shared
multi-service postmortems that only describe another service.

Start with this service's current 30-day SLO status, error-budget consumption,
p99 latency target, and baseline latency from the SQL and script evidence.

Use `override` to set four front-matter fields on this service article directly
from its `slo_budgets` SQL row and `flap_detector.py` paragraph:
- `slo_status`: exact status from `slo_budgets` (`healthy`, `warning`, or `breached`).
- `oncall_risk`: `high` when `slo_status` is `breached` or when a recurring SEV-1/SEV-2 flap pattern is unresolved, `medium` when `slo_status` is `warning` without a recurring flap, and `low` when `slo_status` is `healthy` with no recurring flap.
- `budget_consumed_pct`: numeric `budget_consumed_pct` from `slo_budgets` (for example `118.4`).
- `availability_target_pct`: numeric `target_availability_pct` from `slo_budgets` (for example `99.9`).

Explain any recurring alert or latency signature surfaced by `flap_detector.py`
or `slo_hourly`, and tie it explicitly to its documented root cause in the
facts, including older postmortem workarounds (such as cron jobs or watchdog
settings) and any hypotheses that rollbacks or later logs disproved. Name the
exact release SHAs, timestamps, job names, environment variables, and ticket
IDs that the facts state.

Do not recite raw `ops-log` event IDs (such as `evt-...`) or routine `info`
deployment rows whose `alert_name` is `none`. Write plain prose paragraphs
without bold labels before colons.

Every sentence must cite its supporting evidence (`[^fact_...]`, `[^sql:slo:N]`,
or `[^script:scripts/flap_detector.py:N]`). Do not compute your own sums,
averages, or percentages; use only numbers that appear directly in the cited
evidence.

# Output

## Reliability
