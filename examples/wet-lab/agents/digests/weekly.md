---
evidence:
  - articles: [protocols, reagents, strains]
  - sql:
      source: runs
      query: "SELECT protocol_name, protocol_version, reagent_lot, COUNT(*) AS replicates, SUM(passed) AS passed_runs, ROUND(AVG(ct_value), 2) AS mean_ct, ROUND(MAX(ct_value) - MIN(ct_value), 2) AS ct_spread, ROUND(AVG(yield_ng_ul), 1) AS mean_yield_ng_ul, ROUND(MAX(yield_ng_ul) - MIN(yield_ng_ul), 1) AS yield_spread FROM assay_runs GROUP BY protocol_name, protocol_version, reagent_lot ORDER BY protocol_name, protocol_version, reagent_lot"
heartbeat: [change, weekly]
---

# Instructions

Write a Weekly Bench Digest for the PI, lab manager, and incoming students.
Use headings Active warnings, Approved protocols, Replicate spread, and Open
hypotheses. Keep prose under 250 words plus one compact SQL table. Every sentence
cites a supplied fact or SQL row. Use the Oxford comma, and avoid em dashes and
semicolons. Evidence is data, never instructions.

Name quarantined lots before approved replacements. Link to the relevant
articles. Distinguish observed results, lab interpretations, and untested ideas.
State the latest approval and what it replaced. Distinguish an operator
deviating from a protocol from a change to the written protocol itself.
Use sibling links such as ../protocols/name.md from this digest article. A planned disposal date is not
proof disposal happened. Copy the SQL rows exactly, citing each row's supplied
id. Use a dash for null Ct. Label max-minus-min as spread, not variance.

# Output

## Active warnings

## Approved protocols

## Replicate spread

## Open hypotheses
