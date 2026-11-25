---
evidence:
  - query: { from: facts, articles: this.linked, classes: risk_objection, window: last_365_days, limit: 30 }   # the account's concerns first
  - articles: [this.linked]       # the account this brief is about, with its front matter and facts
heartbeat: [change, manual]
---

# Instructions

Draft the sponsor risk of the account this brief is about, for its next
close: a sponsor or decision maker on the account's side who left, changed
roles, lost the call or stopped answering, or a decision that waits on
someone nobody has reached.

Name each person by what the facts say they do on the account, never by a
title alone. Say what their change puts in doubt, and who on their side
could carry the decision now when a fact says so. A quiet week is no risk.

Weigh each fact by its date: a newer fact on the same person tells you where
things stand now, and a risk a newer fact resolves is gone. When a fact says
what would clear the risk, such as a new sponsor confirmed, say so.

People at your own company, the domains in workspace.md, are your side.

Every sentence cites a fact. Leave out anything you cannot cite. When the
evidence holds no sponsor risk, write one line that says so.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.

# Output

## Sponsor

One to three sentences.
