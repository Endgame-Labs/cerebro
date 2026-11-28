#!/usr/bin/env -S uv run --script
"""Build data/runs.sqlite for the wet-lab example workspace.

Run from the wet-lab workspace folder:
    uv run scripts/build_runs_db.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "runs.sqlite"

SCHEMA = """
CREATE TABLE assay_runs (
    run_id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    protocol_name TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    reagent_lot TEXT NOT NULL,
    strain_name TEXT NOT NULL,
    operator TEXT NOT NULL,
    replicate INTEGER NOT NULL,
    ct_value REAL,
    yield_ng_ul REAL,
    passed INTEGER NOT NULL,
    notes TEXT NOT NULL
);
"""

# fmt: off
ROWS = [
    # CRISPR-Cas12a Lateral Flow Assay — v2.1 baseline with Buffer X Lot 3A (3/3 pass, tight Ct)
    ("RUN-001-R1", "2026-10-12", "CRISPR-Cas12a Lateral Flow Assay", "v2.1", "Buffer X — Lot 3A", "none", "Devon Morales", 1, 21.1, 148.0, 1, "37 C 30 min; crisp test band"),
    ("RUN-001-R2", "2026-10-12", "CRISPR-Cas12a Lateral Flow Assay", "v2.1", "Buffer X — Lot 3A", "none", "Devon Morales", 2, 21.4, 144.0, 1, "37 C 30 min; crisp test band"),
    ("RUN-001-R3", "2026-10-12", "CRISPR-Cas12a Lateral Flow Assay", "v2.1", "Buffer X — Lot 3A", "none", "Devon Morales", 3, 21.6, 151.0, 1, "37 C 30 min; crisp test band"),
    # CRISPR-Cas12a Lateral Flow Assay — v2.2 failure #1 with Buffer X Lot 4B at 42 C (0/3 pass, wide Ct variance)
    ("RUN-002-R1", "2026-10-20", "CRISPR-Cas12a Lateral Flow Assay", "v2.2", "Buffer X — Lot 4B", "none", "Devon Morales", 1, 35.9, 19.0, 0, "42 C 20 min; no test line"),
    ("RUN-002-R2", "2026-10-20", "CRISPR-Cas12a Lateral Flow Assay", "v2.2", "Buffer X — Lot 4B", "none", "Devon Morales", 2, 37.1, 14.0, 0, "42 C 20 min; no test line"),
    ("RUN-002-R3", "2026-10-20", "CRISPR-Cas12a Lateral Flow Assay", "v2.2", "Buffer X — Lot 4B", "none", "Devon Morales", 3, 38.2, 11.0, 0, "42 C 20 min; no test line"),
    # CRISPR-Cas12a Lateral Flow Assay — v2.2 failure #2 with Buffer X Lot 4B and fresh LbCas12a Lot E20 (0/3 pass)
    ("RUN-003-R1", "2026-10-23", "CRISPR-Cas12a Lateral Flow Assay", "v2.2", "Buffer X — Lot 4B", "none", "Soren Lindqvist", 1, 34.8, 22.0, 0, "Fresh LbCas12a Lot E20; precipitate found in Buffer X Lot 4B"),
    ("RUN-003-R2", "2026-10-23", "CRISPR-Cas12a Lateral Flow Assay", "v2.2", "Buffer X — Lot 4B", "none", "Soren Lindqvist", 2, 36.4, 17.0, 0, "Fresh LbCas12a Lot E20; precipitate found in Buffer X Lot 4B"),
    ("RUN-003-R3", "2026-10-23", "CRISPR-Cas12a Lateral Flow Assay", "v2.2", "Buffer X — Lot 4B", "none", "Soren Lindqvist", 3, 38.6, 12.0, 0, "Fresh LbCas12a Lot E20; precipitate found in Buffer X Lot 4B"),
    # CRISPR-Cas12a Lateral Flow Assay — v2.3 validation with Buffer X Lot 5C at 37 C (6/6 pass across Devon & Marco)
    ("RUN-004-R1", "2026-11-04", "CRISPR-Cas12a Lateral Flow Assay", "v2.3", "Buffer X — Lot 5C", "none", "Devon Morales", 1, 21.0, 156.0, 1, "37 C 30 min; Buffer X Lot 5C at 20 C; strong test line"),
    ("RUN-004-R2", "2026-11-04", "CRISPR-Cas12a Lateral Flow Assay", "v2.3", "Buffer X — Lot 5C", "none", "Devon Morales", 2, 21.3, 152.0, 1, "37 C 30 min; Buffer X Lot 5C at 20 C; strong test line"),
    ("RUN-004-R3", "2026-11-04", "CRISPR-Cas12a Lateral Flow Assay", "v2.3", "Buffer X — Lot 5C", "none", "Devon Morales", 3, 21.5, 159.0, 1, "37 C 30 min; Buffer X Lot 5C at 20 C; strong test line"),
    ("RUN-005-R1", "2026-11-12", "CRISPR-Cas12a Lateral Flow Assay", "v2.3", "Buffer X — Lot 5C", "none", "Marco Bellini", 1, 21.2, 154.0, 1, "Onboarding replication; crisp test line and clean NTC"),
    ("RUN-005-R2", "2026-11-12", "CRISPR-Cas12a Lateral Flow Assay", "v2.3", "Buffer X — Lot 5C", "none", "Marco Bellini", 2, 21.4, 150.0, 1, "Onboarding replication; crisp test line and clean NTC"),
    ("RUN-005-R3", "2026-11-12", "CRISPR-Cas12a Lateral Flow Assay", "v2.3", "Buffer X — Lot 5C", "none", "Marco Bellini", 3, 21.7, 155.0, 1, "Onboarding replication; crisp test line and clean NTC"),
    # Delta-Lon Transformation and Expression — v1.3 initial & failed runs (50 ug/mL Kan, 45 s initial / 90 s deviation)
    ("RUN-006-R1", "2026-10-06", "Delta-Lon Transformation and Expression", "v1.3", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Soren Lindqvist", 1, None, 118.0, 1, "50 ug/mL Kan, OD600 0.8 induction; slow 24 h growth"),
    ("RUN-006-R2", "2026-10-06", "Delta-Lon Transformation and Expression", "v1.3", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Soren Lindqvist", 2, None, 112.0, 1, "50 ug/mL Kan, OD600 0.8 induction; slow 24 h growth"),
    ("RUN-006-R3", "2026-10-06", "Delta-Lon Transformation and Expression", "v1.3", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Soren Lindqvist", 3, None, 121.0, 1, "50 ug/mL Kan, OD600 0.8 induction; slow 24 h growth"),
    ("RUN-007-R1", "2026-10-28", "Delta-Lon Transformation and Expression", "v1.3", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Marco Bellini", 1, None, 42.0, 0, "90 s heat shock at 42 C + 50 ug/mL Kan; 12 colonies"),
    ("RUN-007-R2", "2026-10-28", "Delta-Lon Transformation and Expression", "v1.3", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Marco Bellini", 2, None, 58.0, 0, "90 s heat shock at 42 C + 50 ug/mL Kan; 14 colonies"),
    ("RUN-007-R3", "2026-10-28", "Delta-Lon Transformation and Expression", "v1.3", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Marco Bellini", 3, None, 49.0, 0, "90 s heat shock at 42 C + 50 ug/mL Kan; 11 colonies"),
    # Delta-Lon Transformation and Expression — v1.4 validated (45 s heat shock, 30 C SOC, 30 ug/mL Kan, OD600 0.55)
    ("RUN-008-R1", "2026-11-03", "Delta-Lon Transformation and Expression", "v1.4", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Marco Bellini", 1, None, 294.0, 1, "45 s heat shock, 30 C SOC 90 min, 30 ug/mL Kan, OD600 0.55"),
    ("RUN-008-R2", "2026-11-03", "Delta-Lon Transformation and Expression", "v1.4", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Marco Bellini", 2, None, 312.0, 1, "45 s heat shock, 30 C SOC 90 min, 30 ug/mL Kan, OD600 0.55"),
    ("RUN-008-R3", "2026-11-03", "Delta-Lon Transformation and Expression", "v1.4", "Kanamycin 50 mg/mL Stock — Lot K09", "E. coli K-12 Delta-Lon", "Marco Bellini", 3, None, 285.0, 1, "45 s heat shock, 30 C SOC 90 min, 30 ug/mL Kan, OD600 0.55"),
    # 4-Fragment Gibson Assembly — v1.1 (15 min) vs degraded tube vs v1.2 (30 min fresh aliquot)
    ("RUN-009-R1", "2026-10-08", "4-Fragment Gibson Assembly", "v1.1", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Soren Lindqvist", 1, None, 86.0, 0, "50 C 15 min; only 28% correct clones"),
    ("RUN-009-R2", "2026-10-08", "4-Fragment Gibson Assembly", "v1.1", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Soren Lindqvist", 2, None, 94.0, 0, "50 C 15 min; low junction efficiency"),
    ("RUN-009-R3", "2026-10-08", "4-Fragment Gibson Assembly", "v1.1", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Soren Lindqvist", 3, None, 79.0, 0, "50 C 15 min; incomplete 4-fragment assembly"),
    ("RUN-010-R1", "2026-10-30", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A (freeze-thaw x7)", "E. coli DH5alpha-T1R", "Keiko Takahashi", 1, None, 38.0, 0, "Tube #2 after 7 freeze-thaw cycles; 9 colonies"),
    ("RUN-010-R2", "2026-10-30", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A (freeze-thaw x7)", "E. coli DH5alpha-T1R", "Keiko Takahashi", 2, None, 44.0, 0, "Tube #2 after 7 freeze-thaw cycles; 11 colonies"),
    ("RUN-010-R3", "2026-10-30", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A (freeze-thaw x7)", "E. coli DH5alpha-T1R", "Keiko Takahashi", 3, None, 35.0, 0, "Tube #2 after 7 freeze-thaw cycles; 7 colonies"),
    ("RUN-011-R1", "2026-10-30", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Keiko Takahashi", 1, None, 224.0, 1, "50 C 30 min; fresh single-use 10 uL aliquot; 89% correct"),
    ("RUN-011-R2", "2026-10-30", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Keiko Takahashi", 2, None, 231.0, 1, "50 C 30 min; fresh single-use 10 uL aliquot"),
    ("RUN-011-R3", "2026-10-30", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Keiko Takahashi", 3, None, 218.0, 1, "50 C 30 min; fresh single-use 10 uL aliquot"),
    ("RUN-012-R1", "2026-11-10", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Keiko Takahashi", 1, None, 227.0, 1, "Restreaked E. coli DH5alpha-T1R master stock check"),
    ("RUN-012-R2", "2026-11-10", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Keiko Takahashi", 2, None, 222.0, 1, "Restreaked E. coli DH5alpha-T1R master stock check"),
    ("RUN-012-R3", "2026-11-10", "4-Fragment Gibson Assembly", "v1.2", "2x Gibson Master Mix — Lot G11A", "E. coli DH5alpha-T1R", "Keiko Takahashi", 3, None, 233.0, 1, "Restreaked E. coli DH5alpha-T1R master stock check"),
]
# fmt: on


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO assay_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ROWS,
        )
        conn.commit()
    print(f"Wrote {len(ROWS)} rows to {DB_PATH}")


if __name__ == "__main__":
    main()
