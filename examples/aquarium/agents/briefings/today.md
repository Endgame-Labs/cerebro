---
evidence:
  - query: { from: facts, window: last_3_days }   # what the last few shifts wrote, the newest first
  - articles: [tanks, animals]                    # every tank and animal article, as written today
    text_only: true
heartbeat: [daily]
---

# Instructions

This is the briefing a keeper reads when they walk in for the first shift of
the day, before they open any other article. It is written once a day, early,
so it must hold up for every shift that day. Name each day by its date, such
as Friday, November 27, and never as "this morning" or "yesterday": the shift
that reads it may be a day later than the note it rests on.

Start with what needs a keeper today: an animal off its food or acting wrong,
a reading out of range, a vet visit due, a move, or a repair that is not
finished. Give each one in a sentence or two, with the tank and the time or
day the facts give, and cite the fact or the article that says it.

Then what settled in the last three days, one line each, so the keeper does
not chase it.

Then the standing rules that apply on every shift right now, such as a clasp
or a lid check, each in one line.

Then what is coming in the next eight weeks, with dates, from the facts and
the articles: deliveries, installs, replacements and vet rechecks.

Leave out what was resolved more than three days ago and has stayed quiet.
Leave out gift shop, parking and potluck memos. Every sentence cites a fact or
an article, such as [^tanks/tank-4]. Leave out anything you cannot cite. Keep
it short enough to read in two minutes.

# Output

## Needs you today

## Settled

## Every shift

## Coming up
