---
evidence:
  - facts: { window: last_90_days }            # the notes, vet visits, memos and daily water rows tied to this tank
  - query: { from: animals, linked: this }     # the animals that live here, which link this tank
heartbeat: [change]
---

# Instructions

A tank article is what a keeper reads at the start of a shift, standing at the
tank, before the first feeding. They know the building and the routine. They
need to know whether this tank is all right today, what changed lately, and
what they must do or watch. Keep it short: a keeper reads it in a minute.

Lead with the water. Each daily water row gives a temperature, a salinity and
an ammonia reading, and the record it cites names its day, such as
"2026-11-26 Tank 4". Give the readings of the newest day, name that day, and
say whether each sits inside the tank's target range, as the tank targets give
it. When a reading left its range in the last two weeks, say in one or two
sentences when, how far, what the notes say caused it, and when it came back.
When the water has held steady, one sentence says so, citing the oldest and
the newest day's rows only.

Then what changed at the tank, newest first: equipment faults and repairs,
moves in or out, and decisions about the tank or its gear. A later fact on
the same thread wins over an earlier one: when a date moved or a decision was
reversed, give the one that stands and say once that it changed. When people
disagree, say who holds which view and how it came out.

Then the standing rules a keeper must follow at this tank, such as a clasp to
fasten or a tool to keep off the railing, and what is coming, with dates.

Name the animals that live here, from the animals your evidence lists, in one
sentence that cites each animal's article, such as [^animals/marisol]. An
animal's appetite, weight, diet, treatments and vet visits belong in its own
article: leave them out here, except in the one clause that ties a tank event
to it, such as an animal that went off its food while the water ran warm.
Groups the notes describe, such as the leopard sharks, belong here.

Every sentence cites a fact. Leave out anything you cannot cite. Gift shop,
parking and potluck memos say nothing about a tank. Write plainly, the way one
keeper briefs another. Write dates as November 26, with no year unless it is
not 2026. Each fact appears once in the article.

Set exhibit with override to the exhibit's name, as the tank targets give it,
such as Octopus tank.

# Output

## Today

## What changed

## Rules and what's next
