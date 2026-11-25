---
evidence:
  - query: { from: facts, articles: this.linked, classes: risk_objection, window: last_365_days, limit: 30 }   # the account's concerns first
  - articles: [this.linked]       # the account this brief is about, with its front matter and facts
heartbeat: [change, manual]
---

# Instructions

Draft the competitor risk of the account this brief is about, for its next
close: another vendor still in the evaluation, an incumbent the account
compares us with, or a build of their own they weigh against buying.

Name the competitor as the facts name it, and say where it stands in the
evaluation and what the account weighs between us, by what the facts say.
A competitor a newer fact says the account dropped is no longer a risk. When
a fact says what would clear the risk, such as a decision date or a
comparison the account asked for, say so.

People at your own company, the domains in workspace.md, are your side.

Every sentence cites a fact. Leave out anything you cannot cite. When the
evidence holds no competitor, write one line that says so.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.

# Output

## Competitor

One to three sentences.
