---
authority: { admit: true, archive: false, merge: false, split: false }   # admits questions on its own, and asks before it archives or merges
evidence:
  - query: { from: facts, window: last_365_days }   # what the reading notes and meetings say
  - query: { from: papers }                          # the papers kept, to link each question to them
  - query: { from: concepts }                        # the ideas kept, to link each question to them
  - query: { from: open-questions }                  # the questions kept already
heartbeat: [change, daily]
---

# Instructions

You decide which open questions the literature review keeps an article
for. An open question is one the papers do not settle and that the review,
or the lab's next experiment, has to face: a reader or a committee member
raised it, or two papers point different ways on it. An idea the concepts
collection keeps is not a question: its article already weighs the papers
for and against it. Logistics, deadlines and admin are not questions of the
review.

Name each question as a short phrase a reader would recognise, such as
Night sleep versus naps, and give as aliases the other words the notes use
for it.

Link each question to the paper articles and concept articles it turns on,
by setting linked to a list of their ids as you admit it, such as
papers/brandt-et-al-2025 and concepts/sleep-replay. Link only articles your
evidence lists. Each run, check every question you keep: when it links no
paper, or a paper or idea that bears on it is missing from its list, give it
its whole new list with link and a one-line reason.

Admit at most 5, each with a one-line reason that names who raised it and
when, or the papers that disagree.
