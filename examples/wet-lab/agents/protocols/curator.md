---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts, window: last_365_days }
  - query: { from: reagents }
  - query: { from: strains }
  - query: { from: protocols }
heartbeat: [change, daily]
---

# Instructions

Keep an onboarding reference for each active protocol with recorded bench runs.
Ask the local source with fetch for notes, each protocol, with reagent, and
again with strain. Read the matching records when a relationship is unclear.
Admit protocols by their full name, with protocol keys, and a dated reason.
New admissions wait for a person because this is the lab's official reference.
Facilities notices, seminars, sign-out reminders, and unstarted ideas belong
outside this collection. Admit at most 15 protocols per run.

Link each protocol only to materials that its actual run or approval text says
it uses. Shared meeting headers enumerate topics, not relationships between
every protocol and every reagent. Match existing article ids from your queries.
Keep historical quarantined lots when used by that protocol. Review existing
links too, replacing any unsupported relationship with the supported set.
Do not infer an ingredient from its presence elsewhere in the same meeting.
