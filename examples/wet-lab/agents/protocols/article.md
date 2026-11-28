---
subagents:
  - current-protocol
  - known-pitfalls
  - recent-runs
evidence:
  - articles: [this.linked]
---

Write for a new graduate student who needs this protocol's standing rules
and the evidence behind them. Include only facts about this protocol or materials
it actually uses. A shared meeting can contain facts about unrelated protocols.
Every sentence cites a supplied fact or SQL row. Keep prose concise, use the
Oxford comma, and write plain sentences without em dashes or semicolons.

# Composing

Compose a brief reference from the three drafts and linked articles. Lead with
the full protocol name and approved version. Use headings Current protocol,
Linked materials, Known pitfalls, and Reconciled run history. Keep prose to
about 350 words plus one SQL table. Say each rule once.

Give current approved parameters before superseded trials. Name the specific
lot in every warning. Retain only linked materials that the run or approval
text ties to this protocol. A broad meeting header is not such evidence.
Distinguish observed failures, the lab's interpretations, and untested remedies.
Omit unrelated warnings even when a draft included them. Judge scope from
what each claim says, not its attachment to a meeting or article.

Keep the recent-runs table for this protocol, copying figures and citation ids
exactly. Explain briefly why the failures and later passes differ. A range is
max minus min, not statistical variance. Preserve citations from the drafts. Put each SQL row citation inside its
last table cell, before the closing pipe, so it survives composition.
Omit a heading if nothing under it can be supported.
Use override to set protocol_version and approval_status from the latest
approval facts, and linked to the article ids of the materials actually used.

# Output

## Current protocol

## Linked materials

## Known pitfalls

## Reconciled run history
