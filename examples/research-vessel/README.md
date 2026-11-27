# Research Vessel (*R/V Shearwater*)

Build a voyage operations and sensor wiki for chief scientists, bridge officers, and deck engineers aboard a synthetic research vessel (*R/V Shearwater*) on a 30-day North Atlantic survey. Cerebro reads watch logs, a CSV sampling station manifest, and a SQLite telemetry database, then writes instrument readiness articles, station plans, and a daily morning bridge brief. All vessels, crew, logs, and telemetry readings are fictional.

## What's in the folder

```text
examples/research-vessel/
├── settings.yaml               # models, $5 daily budget, UTC time zone
├── workspace.md                # voyage context and readiness states
├── profile.md                  # chief scientist profile
├── AGENTS.md                   # guide for agents working in this workspace
├── facts/kinds.py              # CalibrationDrift, EquipmentFault, SafetyLimit, SamplingChange, Other
├── sources/
│   ├── logs.yaml               # markdown watch logs, calibration notes, and weather advisories
│   ├── stations.yaml           # CSV row source for the sampling station manifest
│   └── telemetry.yaml          # read-only SQLite database for sql evidence
├── logs/                       # 18 original voyage logs (15 operational, 3 noise)
├── data/
│   ├── stations.csv            # 6 North Atlantic sampling stations
│   └── telemetry.sqlite        # 21 per-cast salinity, tension, depth-bias, and battery readings
├── scripts/build_telemetry.py  # rebuilds data/telemetry.sqlite from scratch
├── agents/
│   ├── instruments/            # curator.md and article.md (sets readiness via override)
│   ├── stations/               # curator.md (from held CSV rows) and article.md
│   └── briefings/daily.md      # daily heartbeat bridge and science brief
└── evals/bridge.yaml           # 5-question bridge readiness eval suite
```

## Run it

Install Cerebro from the repository root, trust a copy of this workspace, add your model keys, and run one pass:

```sh
uv tool install .
cerebro trust --new-id examples/research-vessel
cd examples/research-vessel
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes --see-first "CTD Rosette #2, AUV Glider Pelagia, Starboard Traction Winch, Multibeam Echosounder EM304, and all North Atlantic sampling stations"
```

Read what Cerebro wrote under `articles/`, inspect the workspace state, and run the bridge eval suite:

```sh
cerebro status
cerebro eval evals/bridge.yaml
```

To rebuild the SQLite telemetry database yourself, run `python3 scripts/build_telemetry.py` from `examples/research-vessel`.

## What it shows

| Feature | Where it lives | What it does |
| --- | --- | --- |
| Daily heartbeat digest | `agents/briefings/daily.md` | Writes the morning bridge brief on a `daily` and `change` heartbeat, querying instruments by `readiness`. |
| SQLite `database` source and `sql` evidence | `sources/telemetry.yaml`, `agents/instruments/article.md`, `agents/briefings/daily.md` | Runs read-only SQL against `data/telemetry.sqlite` and cites telemetry rows beside watch-log facts. |
| CSV row source with `identity:` and `fields:` | `sources/stations.yaml`, `data/stations.csv` | Turns each row of the station manifest into a held record with `depth_m`, `sea_state`, and `cast_status`. |
| Agent-set front matter via `override` | `agents/instruments/article.md`, `agents/stations/article.md` | Sets `readiness` (`ready`, `degraded`, or `grounded`) on instruments and syncs station fields from facts. |
| Custom fact kinds | `facts/kinds.py` | Sorts voyage facts into `CalibrationDrift`, `EquipmentFault`, `SafetyLimit`, `SamplingChange`, and `Other`. |
| Curator noise filtering | `agents/instruments/curator.md`, `agents/stations/curator.md` | Admits 4 tracked instruments and 6 stations while leaving out galley, fire-drill, and crew Wi-Fi logs. |
| Question eval suite | `evals/bridge.yaml` | Checks readiness fields, key numbers, and bridge-hold answers with `cerebro eval`. |

## Sample output

On a real run (`24` records across `logs/` and `data/stations.csv`, plus `21` rows in `data/telemetry.sqlite`), Cerebro extracted `107` current facts, wrote `11` articles (`1` daily brief, `4` instruments, and `6` stations), scored `1.00` across all `5` checks in `evals/bridge.yaml`, and spent `$0.48` (`$0.04` extraction, `$0.44` compilation, plus `$0.0003` for the eval).

Here is `articles/instruments/ctd-rosette-2.md` verbatim from that run. It connects the `+0.41 PSU` Cast 20 salinity drift in SQLite to a night-watch log about a nicked O-ring, sets `readiness: degraded`, and flags the bulkhead inspection before the planned 4,000-meter Flemish Cap cast:

```markdown
---
id: instruments/ctd-rosette-2
title: 'CTD Rosette #2'
identity:
  instrument:
    - 'CTD Rosette #2'
readiness: degraded
compiled_at: '2026-11-27T02:37:36Z'
---

## Current Readiness

The pre-cast test dip for Cast 20 at Station NA-14 on November 26 logged a +0.41 PSU salinity drift on the primary conductivity cell, against a 0.05 PSU limit, recorded at 11:20 UTC during the 1,200-meter dip on the Aft Hydrographic Winch [^sql:telemetry:3][^fact_01m8ddtm8zrfm3gzq0]. The primary conductivity cell bulkhead connector still carries a small nick on its lower Viton O-ring, spotted on November 24, that Henrik Lindholm re-seated with silicone grease rather than replaced [^fact_01m8ddt6evbfsmeat0][^fact_01m8ddt8n5cc4t5rgq]. Henrik Lindholm noted the nicked O-ring should be swapped before any deep pressure cast, since saltwater weeping into the primary conductivity bulkhead under high hydrostatic pressure will cause rapid salinity drift [^fact_01m8ddt6ev1rwzv1wy]. Per the cruise calibration policy, any cast exceeding 0.05 PSU drift must be flagged and the primary bulkhead inspected before deep casts below 2,000 meters [^fact_01m8ddsdred4knjy6h].

## Telemetry and Calibration

Cast 19 at Station NA-13 on November 25 flagged a +0.24 PSU primary salinity drift below 1,500 meters, above the 0.05 PSU limit, and the station's cast status was marked completed_suspect pending bottle-salinity reconciliation [^sql:telemetry:6][^fact_01m8ddtcvpr0anryh5]. Cast 18 at Station NA-12 on November 24 recorded a 0.031 PSU salinity drift, under the limit but noted as upward drift on the upcast prior to rinse [^sql:telemetry:10]. Earlier casts read within tolerance: Cast 17 at Station NA-11 showed a 0.018 PSU offset between the primary and secondary sensors, which Dr. Priya Subramanian attributed to temporary cell flushing lag in steep thermal gradients, keeping CTD Rosette #2 cleared for the Flemish Pass and Flemish Cap stations [^sql:telemetry:12][^fact_01m8ddszsjkcq0wq77][^fact_01m8ddszsjztwwwwbr][^fact_01m8ddszsj51d874bx]. Cast 15 reached 2,650 meters cleanly with 0.003 PSU drift against deep Autosal bottles [^sql:telemetry:14][^fact_01m8ddst7akhsq3qp9], and Cast 08 on the Grand Banks shakedown returned clean profiles with 0.002 PSU drift against Autosal bottle samples [^sql:telemetry:19][^fact_01m8ddshtsz6p7zaa6]. The Sea-Bird SBE 5T pump current was nominal on both Cast 19 (0.42 A) and the Cast 20 test dip (0.43 A), against a 0.6 A limit [^sql:telemetry:5][^sql:telemetry:2].

## Faults, Limits, and Watch Notes

While disconnecting the primary Sea-Bird conductivity cell cable to flush the deck frame after Cast 18, Henrik Lindholm spotted the small nick on the lower Viton O-ring of the primary conductivity cell bulkhead connector during the 03:15 UTC night-watch rinse on November 24 [^fact_01m8ddt86p741071w9][^fact_01m8ddt6evbfsmeat0]. The bulkhead pin cavity looked dry at surface pressure, so he wiped the groove clean, applied silicone grease, and re-seated the nicked O-ring rather than waking Dr. Priya Subramanian at 03:00 UTC [^fact_01m8ddt8gb20s92kgq]. The spare O-ring kit #SB-44 is in the forward wet-lab locker [^fact_01m8ddt6ev0emxhr4t]. Dr. Priya Subramanian's cruise calibration policy sets 0.05 PSU primary-to-secondary or bottle-check divergence as the operational degradation threshold for CTD Rosette #2 [^fact_01m8ddsdrd843z4cpe]. The Station NA-14 deep 4,000-meter CTD cast was pushed to November 27 at 06:00 UTC to let the cross-swell subside before the afternoon gale [^fact_01m8ddtcvpsvm1603n], and the raw Cast 20 telemetry is uploaded to the shipboard telemetry database awaiting morning QC review [^fact_01m8ddtm8zrfm3gzq0].

[^sql:telemetry:3]: `sql:telemetry`

[^fact_01m8ddtm8zrfm3gzq0]: logs/2026-11-26-flemish-cap-cast-plan.md "At 11:20 UTC, deck crew ran a 1,200-meter pre-cast test dip (Cast 20) with CTD Rosette #2 on the Aft Hydrographic Winch; raw telemetry has uploaded to the shipboard telemetry database awaiting morning QC review."

[^fact_01m8ddt6evbfsmeat0]: logs/2026-11-24-night-watch-ctd-oring.md "While disconnecting the primary Sea-Bird conductivity cell cable to flush the deck frame, Henrik Lindholm spotted a small nick on the lower Viton O-ring of the primary conductivity cell bulkhead connector."

[^fact_01m8ddt8n5cc4t5rgq]: logs/2026-11-24-night-watch-ctd-oring.md "The bulkhead pin cavity looked dry at surface pressure, so Henrik Lindholm wiped the groove clean, applied silicone grease, and re-seated the nicked O-ring rather than waking Dr. Priya Subramanian at 03:00 UTC."

[^fact_01m8ddt6ev1rwzv1wy]: logs/2026-11-24-night-watch-ctd-oring.md "Henrik noted that the spare O-ring kit #SB-44 is in the forward wet-lab locker and the nicked O-ring should be swapped before any deep pressure cast, since saltwater weeping into the primary conductivity bulkhead under high hydrostatic pressure will cause rapid salinity drift."

[^fact_01m8ddsdred4knjy6h]: logs/2026-09-22-ctd-precruise-calibration.md "Our cruise calibration policy sets 0.05 PSU primary-to-secondary or bottle-check divergence as the operational degradation threshold; any cast exceeding 0.05 PSU drift must be flagged and the primary bulkhead inspected before deep casts below 2,000 meters."

[^sql:telemetry:6]: `sql:telemetry`

[^fact_01m8ddtcvpr0anryh5]: logs/2026-11-25-evening-watch-stations.md "Completed Cast 19 at Station NA-13: East Flemish Slope (depth 3,120 meters, sea state 4), though raw post-cast telemetry flagged a +0.24 PSU primary salinity anomaly below 1,500 meters, marking Station NA-13 cast status as completed_suspect pending bottle-salinity reconciliation."

[^sql:telemetry:10]: `sql:telemetry`

[^sql:telemetry:12]: `sql:telemetry`

[^fact_01m8ddszsjkcq0wq77]: logs/2026-11-21-ctd-cast17-offset.md "On the final departure profile at Station NA-11: Orphan Knoll (Cast 17), CTD Rosette #2 primary conductivity showed a 0.018 PSU offset against the secondary sensor during the upcast."

[^fact_01m8ddszsjztwwwwbr]: logs/2026-11-21-ctd-cast17-offset.md "Because 0.018 PSU remained below the 0.05 PSU degradation threshold, Dr. Priya Subramanian attributed the small offset to temporary cell flushing lag in steep thermal gradients and kept CTD Rosette #2 cleared for upcoming Flemish Pass and Flemish Cap stations."

[^fact_01m8ddszsj51d874bx]: logs/2026-11-21-ctd-cast17-offset.md "Because 0.018 PSU remained below the 0.05 PSU degradation threshold, Dr. Priya Subramanian attributed the small offset to temporary cell flushing lag in steep thermal gradients and kept CTD Rosette #2 cleared for upcoming Flemish Pass and Flemish Cap stations."

[^sql:telemetry:14]: `sql:telemetry`

[^fact_01m8ddst7akhsq3qp9]: logs/2026-11-18-orphan-knoll-ops.md "CTD Rosette #2 Cast 15 reached 2,650 meters cleanly with 0.003 PSU salinity drift against deep Autosal bottles."

[^sql:telemetry:19]: `sql:telemetry`

[^fact_01m8ddshtsz6p7zaa6]: logs/2026-11-05-grand-banks-shakedown.md "CTD Rosette #2 Cast 08 returned clean salinity profiles with 0.002 PSU drift against Autosal bottle samples."

[^sql:telemetry:5]: `sql:telemetry`

[^sql:telemetry:2]: `sql:telemetry`

[^fact_01m8ddt86p741071w9]: logs/2026-11-24-night-watch-ctd-oring.md "Night watch log (03:15 UTC) after rinsing CTD Rosette #2 following Cast 18 at Station NA-12: Flemish Pass."

[^fact_01m8ddt8gb20s92kgq]: logs/2026-11-24-night-watch-ctd-oring.md "The bulkhead pin cavity looked dry at surface pressure, so Henrik Lindholm wiped the groove clean, applied silicone grease, and re-seated the nicked O-ring rather than waking Dr. Priya Subramanian at 03:00 UTC."

[^fact_01m8ddt6ev0emxhr4t]: logs/2026-11-24-night-watch-ctd-oring.md "Henrik noted that the spare O-ring kit #SB-44 is in the forward wet-lab locker and the nicked O-ring should be swapped before any deep pressure cast, since saltwater weeping into the primary conductivity bulkhead under high hydrostatic pressure will cause rapid salinity drift."

[^fact_01m8ddsdrd843z4cpe]: logs/2026-09-22-ctd-precruise-calibration.md "Our cruise calibration policy sets 0.05 PSU primary-to-secondary or bottle-check divergence as the operational degradation threshold; any cast exceeding 0.05 PSU drift must be flagged and the primary bulkhead inspected before deep casts below 2,000 meters."

[^fact_01m8ddtcvpsvm1603n]: logs/2026-11-25-evening-watch-stations.md "Station NA-14: Flemish Cap deep 4,000-meter CTD cast, originally planned for November 25 and previously shifted to November 26 at 18:00 UTC due to swell, is pushed again to November 27 at 06:00 UTC to let the cross-swell subside before the afternoon gale."
```
