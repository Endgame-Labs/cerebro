#!/usr/bin/env python3
"""Build data/telemetry.sqlite for the R/V Shearwater example workspace.

Run from the workspace root:
    python3 scripts/build_telemetry.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "telemetry.sqlite"

SCHEMA = """
DROP TABLE IF EXISTS telemetry_readings;

CREATE TABLE telemetry_readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    station_id TEXT NOT NULL,
    cast_no TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    limit_value REAL NOT NULL,
    status TEXT NOT NULL,
    notes TEXT NOT NULL
);
"""

# (instrument, station_id, cast_no, recorded_at, metric, value, unit, limit_value, status, notes)
ROWS = [
    # CTD Rosette #2: per-cast primary vs secondary/Autosal salinity drift (tolerance <= 0.05 PSU)
    ("CTD Rosette #2", "NA-09", "Cast 08", "2026-11-05T13:10:00Z", "salinity_drift", 0.002, "PSU", 0.05, "nominal", "Primary vs Autosal bottle check at 320 m"),
    ("CTD Rosette #2", "NA-11", "Cast 15", "2026-11-18T18:05:00Z", "salinity_drift", 0.003, "PSU", 0.05, "nominal", "Deep bottle check at 2600 m within spec"),
    ("CTD Rosette #2", "NA-11", "Cast 17", "2026-11-21T07:40:00Z", "salinity_drift", 0.018, "PSU", 0.05, "nominal", "Minor offset between primary and secondary sensors"),
    ("CTD Rosette #2", "NA-12", "Cast 18", "2026-11-24T01:55:00Z", "salinity_drift", 0.031, "PSU", 0.05, "warning", "Upward drift noted on upcast prior to rinse"),
    ("CTD Rosette #2", "NA-13", "Cast 19", "2026-11-25T19:40:00Z", "salinity_drift", 0.240, "PSU", 0.05, "critical", "Primary channel jumped +0.24 PSU below 1500 m"),
    ("CTD Rosette #2", "NA-14", "Cast 20", "2026-11-26T11:20:00Z", "salinity_drift", 0.410, "PSU", 0.05, "critical", "Pre-cast test dip to 1200 m; +0.41 PSU drift on primary conductivity cell"),

    # CTD Rosette #2: deck pressure/voltage sanity checks
    ("CTD Rosette #2", "NA-13", "Cast 19", "2026-11-25T19:45:00Z", "pump_current", 0.42, "A", 0.60, "nominal", "Sea-Bird SBE 5T pump draw steady"),
    ("CTD Rosette #2", "NA-14", "Cast 20", "2026-11-26T11:25:00Z", "pump_current", 0.43, "A", 0.60, "nominal", "Sea-Bird SBE 5T pump draw steady at 1200 m"),

    # Starboard Traction Winch: peak line tension readings (SWL safety limit = 60.0 kN)
    ("Starboard Traction Winch", "NA-09", "Core 02", "2026-11-05T10:15:00Z", "peak_tension", 24.5, "kN", 60.0, "nominal", "Box core haul in sea state 3"),
    ("Starboard Traction Winch", "NA-11", "Core 05", "2026-11-18T14:30:00Z", "peak_tension", 41.2, "kN", 60.0, "nominal", "Gravity core haul in 2680 m"),
    ("Starboard Traction Winch", "NA-12", "Core 07", "2026-11-24T15:42:00Z", "peak_tension", 68.4, "kN", 60.0, "critical", "Tension spike during swell snap-load; exceeded 60.0 kN SWL; level-wind sheave pin cracked"),
    ("Starboard Traction Winch", "NA-12", "Core 07", "2026-11-24T15:50:00Z", "hydraulic_pressure", 238.0, "bar", 210.0, "critical", "Relief valve lifted during aborted piston core recovery"),

    # AUV Glider Pelagia: corrected battery pack voltage (hard recovery cutoff = 12.40 V)
    ("AUV Glider Pelagia", "NA-09", "Dive 04", "2026-11-06T06:00:00Z", "battery_voltage", 15.10, "V", 12.40, "nominal", "Post-deployment check on Grand Banks margin"),
    ("AUV Glider Pelagia", "NA-11", "Dive 21", "2026-11-16T08:00:00Z", "battery_voltage", 14.21, "V", 12.40, "nominal", "Retroactively corrected for 4% shunt factor (raw telemetry read 14.80 V)"),
    ("AUV Glider Pelagia", "NA-12", "Dive 34", "2026-11-22T12:00:00Z", "battery_voltage", 12.95, "V", 12.40, "warning", "Cold-core eddy 2C sag; corrected voltage below 13.0 V"),
    ("AUV Glider Pelagia", "NA-13", "Dive 41", "2026-11-25T16:15:00Z", "battery_voltage", 12.65, "V", 12.40, "critical", "Corrected pack voltage 0.25 V above 12.40 V ballast pump stall threshold"),
    ("AUV Glider Pelagia", "NA-14", "Dive 43", "2026-11-26T18:00:00Z", "battery_voltage", 12.58, "V", 12.40, "critical", "Holding shallow loiter at 150 m awaiting 09:00 UTC Nov 27 recovery"),

    # Multibeam Echosounder EM304: bathymetric sounding bias vs check line (tolerance <= 2.0 m)
    ("Multibeam Echosounder EM304", "NA-09", "Line 03", "2026-11-05T09:00:00Z", "depth_bias", 0.4, "m", 2.0, "nominal", "Patch test verified on Grand Banks shelf"),
    ("Multibeam Echosounder EM304", "NA-11", "Line 14", "2026-11-14T22:10:00Z", "depth_bias", 18.2, "m", 2.0, "warning", "Apparent shoaling bias caused by stale autumn sound-velocity profile"),
    ("Multibeam Echosounder EM304", "NA-11", "Line 19", "2026-11-19T11:30:00Z", "depth_bias", 0.3, "m", 2.0, "nominal", "Bias cleared after XBT cast #14 SVP upload"),
    ("Multibeam Echosounder EM304", "NA-14", "Line 28", "2026-11-26T09:15:00Z", "depth_bias", 0.2, "m", 2.0, "nominal", "Flemish Cap 4000 m swath mapping nominal"),
]


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO telemetry_readings "
            "(instrument, station_id, cast_no, recorded_at, metric, value, unit, limit_value, status, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ROWS,
        )
        conn.commit()
    print(f"Wrote {len(ROWS)} rows to {DB_PATH}")


if __name__ == "__main__":
    main()
