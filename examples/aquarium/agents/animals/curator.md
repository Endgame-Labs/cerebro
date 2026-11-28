---
authority: { admit: true, archive: false, merge: false, split: false }   # admits a newly named animal on its own; archives and merges wait for you
evidence:
  - query: { from: facts, window: last_90_days }   # who the notes, vet visits and memos speak of
  - query: { from: tanks }                         # the tanks the wiki keeps, to link each animal to its own
  - query: { from: animals }                       # the animals the wiki keeps, with the tank each links now
heartbeat: [change, daily]
---

# Instructions

Decide which animals get an article. An animal earns one when the team knows
it by name as an individual and cares for it as one: a keeper feeds it, a vet
examines it, or a decision is made about it, such as a move, a diet or a
quarantine. Gus, Marisol and a new arrival with a name are animals. A school,
a colony or a kind of animal, such as the leopard sharks, the moon jellies or
the sea stars, is not: it belongs in its tank's article.

A name on merchandise, a plush toy, a keychain or a sign is not an animal,
even when it borrows an animal's name.

Admit each animal by its name as the keepers write it. Give its species in a
species field, such as "giant Pacific octopus", and link it to the tank it
lives in now, by that tank's article id, with linked, such as
"linked": ["tanks/tank-4"].

Every run, check each animal's link against the newest facts about where it
lives. An animal that moved, out of quarantine or between tanks, links the
tank it lives in now: change it with link, and say in the reason when and
where it went. Leave a note for its writers with instruct only when it says
something the facts do not, and never one that names a tank, since its link
says that.

The people who keep the animals, the vet and the life support technician are
not animals.
