---
evidence:
  - facts: { classes: [identity, relationship, decision_commitment], window: last_365_days }
  - query: { from: people, linked: this }   # the people on this account, as articles
heartbeat: [change, manual]
---

# Instructions

Name the few people your work with this account turns on, and the role each
one plays. Precision over coverage. A wrong role misleads the reader about the
whole account, so an empty role is a finding: "no one who decides is on
record" is exactly what the reader needs to know.

Give a role from what the facts show the person doing, never from what they
are called:

- decides: holds the final call on their side. Signs, owns the budget or the
  approval, gives the go or no-go. A senior title alone is not evidence of it.
- champions: argues inside their own company for more work with us. Bringing
  new people in to meet us, introducing us to the one who decides, arguing for
  us in their own meetings. Doing the day-to-day work with us, coordinating
  logistics or being the named contact is not championing. When you are unsure
  between champions and connects, choose connects.
- connects: in many threads and puts people in touch, without visibly
  driving the decision.
- blocks: actively resists, on explicit evidence: sustained objections,
  pushing an alternative to us, holding back approval. Going quiet is not
  blocking. This role is rare and an empty one is the normal answer.

At most two people in a role, and each person in one role, the one their
facts show best. Attending a meeting or being listed on the account proves
the person is there, not that they hold a role. A role is a claim about what
someone does now: when the behavior that earned it no longer shows in recent
facts, drop the role rather than keep it from an older run.

A person's own facts sit on their article, not on the account's. Before you
write, call query with source facts and filters { articles: <the person's
article id> } for each person the query returned, and weigh what their facts
show. Each fact it returns may be cited by its id.

Name only the people the people query lists. A fact whose label gives its subject as of another account is context, and that subject is never this account's stakeholder.

Before you link a person, find their article, and link only to an article you
found. A person who left is an until, not a removal.

A person of this workspace's own side is never a stakeholder or a contact of
this account, whatever their facts show them doing: one at a domain in
workspace.md, one a fact's label marks "of this workspace's own side", or one
a note or fact places on our side in words. You may name them only as who on
our side worked with a stakeholder, as in "Rosa Delgado met Tomas Weir from our
side".

Every sentence cites a fact. Leave out anyone you cannot cite.

Take a since or until date only from a cited fact that holds it, and when none does, give the role with no date.
A fact's at is the day it was recorded, never a since date: a since or until comes only from the words of a fact's text, such as "joined in March 2026".
Open your content with the first person's sentence, with no summary of the relationship before it.

When the sentence before named more than one person, name the person again rather than writing he, she or they.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.

# Output

## Stakeholders

One sentence per person, the one who decides first: a link to their article
when they have one, such as [Jane Doe](../people/jane-doe.md), their role on
this account, and since or until when a cited fact gives that date. When no
one holds a role, write the sentence that says so.
