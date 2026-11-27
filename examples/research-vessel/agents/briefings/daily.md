---
evidence:
  - query: { from: instruments, readiness: grounded }
  - query: { from: instruments, readiness: degraded }
  - query: { from: instruments, readiness: ready }
  - query: { from: stations }
  - query: { from: facts, classes: calibration_drift, window: last_30_days }
  - query: { from: facts, classes: equipment_fault, window: last_30_days }
  - query: { from: facts, classes: safety_limit, window: last_30_days }
  - query: { from: facts, classes: sampling_change, window: last_30_days }
  - sql:
      source: telemetry
      query: "SELECT instrument, station_id, cast_no, recorded_at, metric, value, unit, limit_value, status, notes FROM telemetry_readings WHERE status != 'nominal' ORDER BY recorded_at DESC"
heartbeat: [daily, change]
---

# Instructions

Write the **Daily Bridge and Science Brief** for the master and chief scientist before the 06:00 UTC morning watch meeting aboard *R/V Shearwater*.

Under `## Gear Readiness and Immediate Holds`, start with a markdown table listing every instrument returned by your `instruments` queries (grounded first, then degraded, then ready) with columns `Instrument` and `Readiness`. Link each instrument to its article (such as `[Instrument Name](../instruments/instrument-name.md)`) and copy its exact `readiness` value from front matter; carry no footnotes inside table rows.

Directly below the table, write one concise bullet for each instrument. Start every bullet with a short cited sentence naming the instrument and its readiness from front matter, citing its article (for example, `Instrument Name has readiness degraded [^instruments/instrument-name].`), then follow with cited sentences from the watch-log facts and `telemetry` SQL rows:
- For every `grounded` or `degraded` instrument, state the non-nominal telemetry reading and its safety or calibration limit from `telemetry`, connect it explicitly to any physical fault, seal damage, or calibration error logged on watch, name any spare part or required inspection, and state which upcoming station cast or recovery is held or affected.
- For every `ready` instrument, note in one cited sentence whether an earlier calibration anomaly was cleared.

Under `## Upcoming Stations and Weather Window`, lead with scheduled and deferred stations, giving their target depths, sea states, shifted cast times, and shore weather routing cutoffs, then briefly note aborted, suspect, and completed stations.

Under `## Watch Decisions and Corrections`, summarize watch disagreements over station timing or recovery and any earlier calibration or telemetry figures that later records corrected, without repeating full details already covered above.

Use only the three headings under `# Output`. Every prose sentence must cite a fact (`[^fact_...]`), a telemetry SQL row (`[^sql:telemetry:...]`), or an article (`[^instruments/...]`, `[^stations/...]`). Do not compute totals, counts, or sums of your own. Leave out anything you cannot cite.

# Output

## Gear Readiness and Immediate Holds

## Upcoming Stations and Weather Window

## Watch Decisions and Corrections
