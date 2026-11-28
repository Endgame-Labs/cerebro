---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - held: {}
  - query: { from: facts, window: last_90_days }
heartbeat: [change, daily]
---

# Instructions

Decide which oceanographic sampling stations get articles in `stations`.

Your held evidence lists the station rows from the sampling station manifest (`data/stations.csv`). Admit each held station by its held record so its `station_id` key binds to the article, using the station's full name as written in the manifest (such as `Station NA-14: Flemish Cap`) and a one-line reason naming its station code and manifest date (do not guess a cast status if it is not shown in your evidence).

Leave out ports, vessels, broad geographic regions without a station code, instruments, crew members, and unrelated shipboard logs (galley provisioning, safety drills, and crew Wi-Fi notices).

Admit at most 20 stations in one run.
