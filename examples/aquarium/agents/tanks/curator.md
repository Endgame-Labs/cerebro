---
authority: { admit: false, archive: false, merge: false, split: false }   # a new tank waits for you in cerebro review
evidence:
  - held: {}                                       # the daily water rows, one per tank per day, not yet on a tank's article
  - query: { from: facts, window: last_90_days }   # what the notes and memos say about each tank
heartbeat: [change, daily]
---

# Instructions

Decide which tanks get an article. A tank is a body of water the team keeps
animals in and tests every day: each daily water row your held list shows
names one, by its tank key, such as Tank 4.

Admit each tank once, by one of its water rows, so every later row of that
tank lands on the same article. Name it as the rows do, such as Tank 4, and
give its exhibit name, as the notes and the tank targets write it, in an
exhibit field, such as "Octopus tank". A quarantine tank behind the scenes is
a tank too, while it is in use or has been in use this season.

A gallery, a sump, a chiller, a filter or a kreisel is part of a tank, not a
tank of its own. The gift shop, the parking lots and the break room are not
tanks.
