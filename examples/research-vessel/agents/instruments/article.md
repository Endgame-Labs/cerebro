---
evidence:
  - facts: { window: last_90_days }
  - sql:
      source: telemetry
      query: "SELECT instrument, station_id, cast_no, recorded_at, metric, value, unit, limit_value, status, notes FROM telemetry_readings ORDER BY recorded_at DESC"
heartbeat: [change, manual]
---

# Instructions

Write a concise operational readiness brief on this instrument for the chief scientist, bridge officers, and deck engineers aboard *R/V Shearwater*.

Combine the watch-log and calibration facts linked to this instrument with its rows from the `telemetry` SQL evidence. Read only the SQL rows whose `instrument` column matches this article's title, and ignore rows for other instruments and facts about unrelated stations.

Set the front-matter field `readiness` using `override` to one of three exact values:
- `grounded`: the instrument is tagged out of service, structurally damaged, or barred from use for the rest of the leg after exceeding a safety limit.
- `degraded`: the instrument exceeds calibration tolerance, approaches a hard safety cutoff, has unresolved physical seal or sensor damage, or requires immediate early recovery or repair before normal deep deployment.
- `ready`: the instrument is within calibration and safety limits with no open faults (including after an earlier anomaly has been verified fixed).

Write plain, directly verifiable statements so every sentence stands up against its citations:
- Do not write a bare label sentence such as "This instrument is degraded" unless a cited fact states that word; let the `readiness` front-matter field carry the label, and lead `## Current Readiness` directly with the concrete telemetry breach, physical fault, tag-out, or verified nominal reading that establishes its current state.
- When a telemetry anomaly (such as salinity drift, battery voltage sag, or tension spike) connects to a watch-log observation (such as a nicked O-ring, shunt calibration error, or cracked pin), state both in tight sequence and cite the exact fact (`[^fact_...]`) and telemetry row (`[^sql:telemetry:...]`) for each claim.
- When a later calibration or watch log corrects an earlier reading or resolves a watch disagreement, state the verified current state first and explain what changed.
- Use only the three headings under `# Output`. Say each fact once rather than repeating the same incident across multiple headings. Leave out anything you cannot cite.

# Output

## Current Readiness

## Telemetry and Calibration

## Faults, Limits, and Watch Notes
