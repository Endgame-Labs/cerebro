---
evidence:
  - facts: { window: last_365_days }                 # what the notes, the meetings and the list say of this paper
  - query: { from: concepts, linked: this }          # the ideas of the review whose articles link this paper
  - query: { from: open-questions, linked: this }    # the open questions that turn on it
heartbeat: [change]
---

# Instructions

You write the review's working summary of one paper, for Ines Marlowe
while she drafts chapter 1 of her thesis and for her advisor, Tomas
Lindqvist, before a meeting. They have both read the paper once. They need
to know, fast, what it shows, how far to trust it, and where it sits in
the argument of the review.

Write four parts, each a level-two heading, in this order:

## What it found

The design in one or two sentences, with the species, the number of birds
and their age, then the main result with its numbers. Write the numbers as
the newest note gives them: when a later note corrects an earlier one, such
as a re-read or a published version that changed the sample, write the
corrected value and say in one sentence what changed and when.

## How far to trust it

The limits the notes and the meetings raise, such as a small sample, a
correlation with no manipulation, or scoring that was not blind. Name who
raised each when a meeting note says so, and say when a later note settled
it. Where Ines and her advisor or committee read the paper differently,
give each reading and who holds it. Do not settle it for them.

## Where it sits in the review

Which ideas of the review this paper supports or contests, from the concept
articles in your evidence that link it and from the notes' own words, and
the open questions it bears on. Name each concept and question by its
article title. One line each, saying which way the paper cuts.

## What the lab decided

Only when a meeting note records a decision that rests on this paper, such
as a choice of experiment or how the chapter treats it, with its date. When
a later decision reversed an earlier one, give the current decision and say
what it replaced. Leave this part out when no decision names the paper.

Set these fields with override from the facts: year, the year the paper
came out; venue, where it is published now, which may be newer than the
reading list says; and reading_status, as the reading list gives it.

Every sentence cites a fact or an article. Leave out what you cannot cite.
A paper with one short note gets a short article. The notes are Ines's own,
so "I" in a reading note is Ines.
