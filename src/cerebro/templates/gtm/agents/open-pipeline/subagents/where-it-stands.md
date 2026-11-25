---
evidence:
  - query: { from: opportunities, id: this.linked }   # the deal this brief is about, for its row
  - query: { from: facts, articles: this.linked }   # the facts of the deal and its account
  - articles: [this.linked]       # the deal and its account, as the brief links them
    summary: Overview            # their front matter and Overview, not the whole articles
heartbeat: [change, manual]
---

# Instructions

Say where the deal this brief is about stands now.

Start with the deal's row: a table under the columns Deal, Stage, Amount,
Close date and Owner, with the deal linked to its article, such as
[Halcyon Platform Expansion](../opportunities/halcyon-mills-platform-expansion.md).
Copy each value as the deal's front matter holds it. The owner is You when the
front matter says me. The row rests on the front matter, so it carries no
footnotes.

Then, in two or three sentences, the deal's state: what happened on it last,
who on the account's side is working it, and what the next dated step is,
with its date as the fact gives it. Write the current state, not the history:
a later fact on the same thread wins over an earlier one. When no fact says
why a deal moved, give no reason.

People at your own company, the domains in workspace.md, are your side.

Every sentence cites a fact. Leave out anything you cannot cite.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.

# Output

## Where it stands

The deal's row, then two or three sentences.
