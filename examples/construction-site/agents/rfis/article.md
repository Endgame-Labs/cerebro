---
evidence:
  - facts: {}                       # the RFI's log row, and what the site reports and mail say of it
  - query: { from: subcontractors } # the subcontractor articles, so the RFI links to its own
heartbeat: [change]
---

# Instructions

Write a short note on one RFI for the project manager, in three to five
sentences, each citing a fact: what was asked and why it came up on site,
who has the ball, what work it holds up, and the answer when there is one.

Keep the RFI's fields current with override, each from the newest fact that
gives it. The log row is only as new as its date: a later message or daily
report outranks it.

- state: open, answered, closed, or void. An RFI the architect has answered
  is answered, even when the log still says open.
- due: the date the answer is due, as YYYY-MM-DD. When a later message moves
  it, use the new date.
- answered: the date the answer was issued, as YYYY-MM-DD, when it was.
- subcontractor: the company that raised it.
- linked: a list holding the one id of that subcontractor's article, from
  your query, such as subcontractors/ridgeline-steel. Leave linked as it is
  when your query holds no article for them.
