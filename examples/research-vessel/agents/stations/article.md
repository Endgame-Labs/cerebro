---
evidence:
  - facts: { window: last_90_days }
  - query: { from: instruments }
  - sql:
      source: telemetry
      query: "SELECT instrument, station_id, cast_no, recorded_at, metric, value, unit, limit_value, status, notes FROM telemetry_readings ORDER BY recorded_at DESC"
heartbeat: [change, manual]
---

# Instructions

Write a concise station operations brief for the chief scientist and bridge watch aboard *R/V Shearwater*.

Keep this station's front matter current from its newest facts using `override`: set `depth_m`, `sea_state`, and `cast_status` to the values the station manifest and latest watch logs establish.

Write only about this station. Ignore facts and telemetry rows in your evidence that belong solely to other stations.

Under `## Station Status`, state this station's coordinates (if given), depth, sea state, and cast status in one or two crisp sentences, without repeating the same field twice.

Under `## Sampling Plan and Changes`, cover schedule shifts, completed or aborted casts, early recoveries, and watch disagreements that directly involve this station.

Under `## Gear and Weather Constraints`, check the `instruments` query and the `telemetry` SQL rows whose `station_id` matches this station:
- For each instrument active or planned at this station, state its readiness exactly as its front matter holds it and cite its article (for example, `Instrument Name has readiness degraded [^instruments/instrument-name].`).
- Put each numeric telemetry reading for this station in its own sentence or clause citing its exact `[^sql:telemetry:...]` row, noting whether it is nominal, warning, or critical against its limit.
- Summarize the weather routing window and wind or sea-state safety limits that apply to this station.

Use only the three headings under `# Output`. Every sentence must cite a fact (`[^fact_...]`), a telemetry SQL row (`[^sql:telemetry:...]`), or an article (`[^instruments/...]`). Leave out anything you cannot cite.

# Output

## Station Status

## Sampling Plan and Changes

## Gear and Weather Constraints
