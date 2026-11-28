---
evidence:
  - facts: { classes: [measurement], window: last_365_days }
  - sql:
      source: runs
      query: "SELECT protocol_name, protocol_version, reagent_lot, COUNT(*) AS replicates, SUM(passed) AS passed_runs, ROUND(AVG(ct_value), 2) AS mean_ct, ROUND(MAX(ct_value) - MIN(ct_value), 2) AS ct_spread, ROUND(AVG(yield_ng_ul), 1) AS mean_yield_ng_ul, ROUND(MAX(yield_ng_ul) - MIN(yield_ng_ul), 1) AS yield_spread FROM assay_runs GROUP BY protocol_name, protocol_version, reagent_lot ORDER BY protocol_name, protocol_version, reagent_lot"
heartbeat: [change, manual]
---

# Instructions

Draft the recent run results and replicate variance record for THIS protocol
alone.

Filter the SQL rows in your evidence to only the rows whose `protocol_name`
matches this protocol. Present those matching rows in a concise markdown table
showing Version, Reagent lot, Replicates, Passed, Mean Ct (or `-` when null),
Ct spread (or `-` when null), Mean yield (ng/uL), and Yield spread (ng/uL),
citing each row's exact `[^sql:runs:N]` id in the row or in a sentence right
below it.

Beside the table, write two to four crisp sentences summarizing the progression
from baseline and failing runs to the validated runs, citing both the
`[^sql:runs:N]` rows and the notebook measurement facts (`[^fact_...]`). Copy
every number exactly as the SQL row or fact states it. Never compute your own
totals, averages, or percentages.

# Output

## Recent runs
