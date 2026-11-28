---
evidence:
  - facts: { classes: [medication, plan, diagnosis] }
  - facts: { classes: [finding], window: last_60_days }
heartbeat: [change, manual]
---

# Instructions

Say what this animal is on today and what is booked next, for a clinician about to
see it. Judge what is active as of today, not as of the visit that set it.

List each active medication, diet, or supplement as one bullet that opens with
the drug and its current dose: the dose with its units, the route, how often, and
the date the latest fact set it, such as "Enalapril 2.5 mg by mouth every 12
hours, set on 4 March". Write "set on" a date, not "since", which claims the
animal took it every day after, more than a note can show. Read every fact on a
drug before you write its bullet, and cite each fact a word of the bullet rests
on: when the route or the strength comes from an earlier note than the dose, cite
that note's fact too.

When a later fact corrects an earlier dose, add a second sentence to the same
bullet that names the dose it replaced, who corrected it, and on which date, such
as "The 5 ml on the 2 March label was a charting error, corrected by Dr. Lee on 3
March." When a dose was raised or lowered, give the one before it in a sentence of
its own. Each sentence is checked against the facts it cites, so a sentence that
carries one claim stands or falls alone.

A drug that was stopped gets no bullet, unless it was stopped in the last 60
days: then one bullet says it was stopped, when, and why. A course with a set
length, such as for 3 days, ended once those days passed, and gets no bullet.
Neither does a step done in the clinic, such as fluids given at a visit.

Then one short paragraph on the active problems the drugs treat and how the
latest finding says each is going, with its value and date.

Then the next booked step: a recheck, test, procedure, or referral, with its date.
When a booking moved or was cancelled, give the date that stands now and say what
it moved from.

Plain bullets and sentences, with no bold labels and no label before a colon,
such as Diet: or Next step:. Every sentence and every bullet cites a fact. Leave
out anything you cannot cite.

# Output

## Current care

A bullet per drug, diet, or supplement, then the paragraph on the problems, then
the next booked step.
