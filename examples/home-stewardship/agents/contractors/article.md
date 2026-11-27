---
evidence:
  - facts: { window: last_365_days }
  - sql: { source: expenses, query: "select invoice_number, invoice_date, system, contractor, amount_usd, warranty_until, summary from invoices order by invoice_date desc" }
heartbeat: [change, manual]
---

# Instructions

Write a concise contractor reference profile for 418 Cedar Hill Road, so either
homeowner or a future owner knows who to call, how they bill, and what work they
have done and quoted on the house.

Under Contacts and terms, write plain complete sentences (avoid short label
prefixes like "Dispatch:") covering the named technicians or foremen to request,
email and phone, payment terms, permitting practices, workmanship warranties,
and house access notes.

Under Work history and open quotes, filter the SQL `invoices` rows strictly to
this contractor (ignore rows for other contractors) and show their invoiced jobs
in one compact markdown table (`Invoice`, `Date`, `System`, `Amount`,
`Warranty`, `Work`), citing each SQL row. Below the table, briefly state only
current open quotes, phase splits, and scheduled visits without repeating every
invoiced row or static system spec. Do not compute totals of your own; report
only amounts stated in rows or facts. Every sentence or table row cites its
fact or SQL row.

# Output

## Contacts and terms

## Work history and open quotes
