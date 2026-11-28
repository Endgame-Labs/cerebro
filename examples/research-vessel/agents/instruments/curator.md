---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts, window: last_90_days }
heartbeat: [change, daily]
---

# Instructions

Decide which scientific instruments and heavy over-the-side deck machinery aboard *R/V Shearwater* get articles in `instruments`.

Ask the `logs` source with `fetch` using `header_values` (`word: instrument`) to list the instruments named in watch-log and calibration headers, and cross-check against the facts in your evidence.

Admit an instrument or deck machine when the logs actively track its calibration, deployments, telemetry, or faults (for example, CTD rosettes, autonomous gliders, multibeam echosounders, and heavy coring winches). Give each article the exact name used in the log headers, with a one-line reason naming what the logs track for it.

Leave out:
- Equipment mentioned only in passing as an unaffected backup with no calibration, fault, or telemetry tracking of its own.
- Ship hotel, galley, crew welfare, personal communications, and routine lifeboat-drill gear (such as espresso machines, walk-in freezers, Starlink terminals, and lifeboat davits).
- People, sampling stations, regions, and vessels.

Admit at most 15 instruments in one run. When a name is ambiguous, note it in your last line instead of guessing.
