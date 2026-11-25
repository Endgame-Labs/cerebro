---
evidence:
  - query: { from: facts, articles: this.linked, classes: risk_objection, window: last_365_days, limit: 30 }   # the account's concerns first
  - articles: [this.linked]       # the account this brief is about, with its front matter and facts
heartbeat: [change, manual]
---

# Instructions

Draft the approval path risk of the account this brief is about, for its
next close: a security, legal or procurement review, a board or committee
approval, or a signature the close waits on, and an objection nobody
answered.

For each, say what it waits on and the date it has to beat when a fact gives
one. A review a newer fact says passed is no longer a risk. When a fact says
what would clear it, such as a document the reviewers asked for, say so.

People at your own company, the domains in workspace.md, are your side.

Every sentence cites a fact. Leave out anything you cannot cite. When the
evidence holds nothing on the approval path, write one line that says so.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.

# Output

## Approval path

One to three sentences.
