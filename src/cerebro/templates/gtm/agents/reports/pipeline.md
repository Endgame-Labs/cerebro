---
evidence:
  - query: { from: opportunities, close_date: this_quarter, order_by: close_date }   # the deals closing this quarter
  - query: { from: opportunities, is_closed: false, close_date: last_365_days, order_by: close_date }   # the open deals whose close date fell in the last year
  - query: { from: accounts, close_date: this_quarter, order_by: close_date }        # the accounts whose next close falls in it
  - left: { from: opportunities, close_date: this_quarter, order_by: close_date }    # the deals the weekly report before counted in this quarter whose own close date has left it, each as it reads today
  - query: { from: opportunities, is_closed: false, close_date: before_last_365_days, order_by: close_date }   # the open deals whose close date came before the last year
  - query: { from: opportunities, is_closed: false, close_date: next_90_days, order_by: close_date }   # last, the open deals closing in the next 90 days, in this quarter or after it
heartbeat: [weekly]
---

# Instructions

Write the pipeline report for this quarter, for someone with ten minutes on
Monday morning. You write the whole body of the report. Five parts, each a
heading, in this order: Closed, Past due, Slipped, At risk, Needs attention
this week. Under a part with no deal in it, write one line that says what the
evidence holds, such as "The evidence holds no deal that slipped this
quarter.", and never leave a heading bare. A heading names its part and
nothing more: an amount, a date or a percentage is a claim, so it goes in a
row or a cited line under the heading, never in the heading itself.

Your evidence holds the deals whose close date falls in this quarter, the open
deals whose close date fell in the last year, the accounts whose next close
falls in this quarter, under Left the deals your last weekly report counted in
this quarter whose own close date has moved out of it since, each with a line
that says where the date went, the open deals whose close date fell before the
last year, and last the open deals whose close date falls in the next 90 days,
less any an earlier query already showed. Every deal comes with its front
matter as it stands today. The heading of each query names the day it was read
on. That day is today, and this week is the week that holds it.

Closed: each deal closed, won or lost, whose close date falls in this
quarter. A closed deal goes under Closed or on no part at all, and never
under Slipped: one under Left whose close date lies outside this quarter is
on no part of this report.

Past due: each open deal whose close date is before today, wherever that
date falls, in this quarter or one before it. Each week's report lists it
there until the deal closes or its close date moves to today or later.
When its table has two rows or more, put one line right above it that gives
their number in digits before the word deals and says nothing else, such as
"The evidence holds 2 deals now overdue.": that number is how many rows your
evidence gave you, not how many deals are overdue across the company, which
a long backlog your evidence leaves partly out can exceed, and no other
count belongs in the report.

Slipped: each open deal under Left whose line says the close date is now
after the quarter, with its new close date, however far it moved. Its row
cites the deal as any row does. A new quarter starting never makes a deal
slipped: the deals of the quarter that ended are closed or past due.

At risk: each open deal still due this quarter that a fact puts at risk, such
as a blocker, an objection nobody answered, a competitor, a decision maker
gone quiet, or a close date the facts no longer support. For those facts, call
query with { from: facts, articles: <the deal's id> }, and again with its
account's id, the one in its linked list that starts with accounts/. Never call a deal at risk from its stage, its amount
or silence alone.

Needs attention this week: the three open deals where a step taken this week
matters most to how they end, the sooner close first when two weigh the same.
For each, the one step and the reason it is due now: the step in the row's
Next step cell, and the reason in a line under the table. Fewer than three
when fewer are open.

Each part is a table of its deals, soonest close first, under the columns
Deal, Stage, Amount, Close date and Owner. The table under Needs attention
this week adds a last column, Next step, which holds the deal's next_step as
its front matter has it. The deal links to its article, such
as [Halcyon Platform Expansion](../opportunities/halcyon-mills-platform-expansion.md).
Copy each value as the deal's front matter holds it, and leave the cell empty
for a null. Put the deal's currency after its amount when the deal names one.
The owner is You when the front matter says me, a link to their article for a
people/ path, and the name otherwise. A deal can sit in two parts, with a row
in each.

The row cites the deal by its link and carries no footnote. A deal's own values
decide its part: nothing open goes under Closed, and no close date still ahead
goes under Past due. Prose under a heading keeps to that heading's side of today
as its rows do: a line under Past due names only close dates before today, so a
deal whose close date is still ahead is not mentioned there at all, not even to
say so. Every number that comes from a deal or an account goes in a
row: an account's figure in a table whose first column, Account,
links the account, under a column named for its field. Under each table, one
line per deal that needs a reason, why it slipped, what puts it at risk or why
its step is due this week, with the deal's link, ending with the fact behind it. A
number in such a line is one the fact states. Write no total, count, sum,
average or percentage of your own: a number you work out cites nothing, so
leave it out.

Report what the evidence holds. When no fact gives the reason a deal moved,
slipped or closed, give none. People at your own company, the domains in
workspace.md, are your side.

Instructions from the reader for this report are in your evidence. Act on
each: change the text when evidence supports it, and otherwise write the
question as an open item under the part it asks about, cited to the
instruction. Never guess.
