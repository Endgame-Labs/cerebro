---
evidence:
  - query: { from: opportunities, linked: this, order_by: close_date }   # this account's deals, as articles
heartbeat: [change, manual]
---

# Instructions

List this account's deals from the evidence: one row per deal the query
returned, and never a deal it did not return. Open deals first, by close date,
soonest first. Then closed deals, by close date, latest first.

When the evidence says some of these deals were not loaded, read them by their
paths before you write, twenty to a read, so the table holds every deal the
query returned.

Copy each value as the deal's front matter holds it. Leave a cell empty when
the field is null. When the deal's pinned list names a field, a person
corrected it against Salesforce: write (corrected) after that cell's value.
The rows rest on the deals' front matter, not on facts, so they carry no
footnotes, and your content holds no sentence about its rows. An open item
cited to an instruction from the reader is the one exception. When the query
returned no deal, write nothing.

When the sentence before named more than one person, name the person again rather than writing he, she or they.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.

# Output

## Opportunities

A markdown table with the columns Name, Stage, Amount, Close date, Owner and
Next step. The name links to the deal's article, such as
[Halcyon Mills renewal](../opportunities/halcyon-mills-renewal.md). The amount carries
its currency when the deal names one. The owner is You when the front matter
says me, a link to their article for a people/ path, such as
[Jane Doe](../people/jane-doe.md), and the name otherwise.
