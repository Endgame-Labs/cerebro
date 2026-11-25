---
evidence:
  - query: { from: opportunities, linked: this.linked, is_closed: false, order_by: close_date }   # the account's open deals, with their close dates
  - articles: [this.linked]       # the account this brief is about, with its front matter and facts
heartbeat: [change, manual]
---

# Instructions

Draft the timing and budget risk of the account this brief is about, for its
next close: a budget that is not approved or moved to another period, a
condition on price or terms that moves the close if it is missed, or a close
date the facts no longer support.

Say what the close depends on and by when, with the date as the fact gives
it. A close date already gone by means the deal slipped: say so, and give the
new date only when a fact states one. When a fact says what would clear the
risk, such as a budget meeting on a given day, say so.

People at your own company, the domains in workspace.md, are your side.

Every sentence cites a fact. Leave out anything you cannot cite, and work
out no count or total of your own. When the evidence holds nothing on timing
or budget, write one line that says so.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.

# Output

## Timing and budget

One to three sentences.
