---
evidence:
  - facts: { window: last_365_days }
  - sql: { source: expenses, query: "select invoice_number, invoice_date, system, contractor, amount_usd, warranty_until, summary from invoices order by invoice_date desc" }
heartbeat: [change, manual]
---

# Instructions

Write the chronological service, repair, and diagnostic history for this system
alone, newest first.

Filter the SQL `invoices` rows strictly to rows whose `system` matches this
article; ignore rows for other house systems. Present invoiced visits in one
compact markdown table (`Date`, `Invoice`, `Contractor`, `Amount`, `Warranty`,
`Work`), citing each SQL row (`[^sql:expenses:N]`). Below the table, add brief
prose only for diagnostic root-cause corrections, settled trade disagreements
(such as a primer test or shading diagnosis), or monthly meter readings that
the table cells do not already state, citing the facts behind them. Do not
repeat each table row as a separate paragraph below the table.

Do not compute totals or sums of your own across rows; report only amounts that
a SQL row or fact states directly. Do not repeat static specifications or
upcoming quotes that other parts own. Write plain sentences without short
colon-label prefixes. Every sentence or table row cites the fact or SQL row
behind it.

# Output

## Service history
