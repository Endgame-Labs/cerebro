#!/usr/bin/env python3
"""Build data/expenses.sqlite for the home-stewardship example workspace.

Run from examples/home-stewardship/:
    python3 scripts/build_expenses_db.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

INVOICES = [
    (
        "INV-2024-0920",
        "2024-09-20",
        "Main Electrical Panel",
        "Willamette Valley Electric",
        4850.00,
        "2029-09-20",
        "200A Square D QO 40-space panel upgrade and dual copper grounding rods",
    ),
    (
        "INV-2024-1104",
        "2024-11-04",
        "Hydronic Boiler and Basement Zones",
        "Alder and Sons Plumbing",
        310.00,
        "2025-11-04",
        "Annual boiler tune-up, Taco circulator lubrication, and 3-zone radiator bleed",
    ),
    (
        "INV-2025-0511",
        "2025-05-11",
        "Rooftop Solar Array",
        "Willamette Valley Electric",
        18400.00,
        "2035-05-11",
        "6.8 kW rooftop solar install (17x REC Alpha Pure 400W + Enphase IQ8M microinverters)",
    ),
    (
        "INV-2025-1019",
        "2025-10-19",
        "Hydronic Boiler and Basement Zones",
        "Alder and Sons Plumbing",
        295.00,
        "2026-10-19",
        "Fall combustion analysis, burner cleaning, and expansion tank check",
    ),
    (
        "INV-2026-0412",
        "2026-04-12",
        "Exterior Envelope and Paint Schedule",
        "Cascadia Heritage Carpentry and Paint",
        3680.00,
        "2028-04-12",
        "South and west sash glazing putty, Kilian bronze pulley replacement, and White Dove trim touch-up",
    ),
    (
        "INV-2026-0918",
        "2026-09-18",
        "Hydronic Boiler and Basement Zones",
        "Alder and Sons Plumbing",
        240.00,
        "2027-09-18",
        "Pre-season 3-zone bleed, manifold inspection, and brass return valve #3 check",
    ),
    (
        "INV-2026-1008",
        "2026-10-08",
        "Main Electrical Panel",
        "Willamette Valley Electric",
        195.00,
        "",
        "Diagnostic visit: attic Circuit 14 knob-and-tube discovery and NEC EV load calculation",
    ),
    (
        "INV-2026-1014",
        "2026-10-14",
        "Rooftop Solar Array",
        "Willamette Valley Electric",
        0.00,
        "",
        "Remote Enphase IQ8M microinverter #11 fault check (courtesy review, confirmed tree shading)",
    ),
    (
        "INV-2026-1020",
        "2026-10-20",
        "Exterior Envelope and Paint Schedule",
        "Cascadia Heritage Carpentry and Paint",
        1920.00,
        "2028-10-20",
        "West kitchen sill rot replacement, Penetrol + oil-based primer, and Roycroft Pewter clapboard match",
    ),
    (
        "INV-2026-1022",
        "2026-10-22",
        "Hydronic Boiler and Basement Zones",
        "Alder and Sons Plumbing",
        185.00,
        "2027-10-22",
        "Amtrol Extrol #30 expansion tank pre-charge raised to 15 psi and Spirovent cap tightened",
    ),
]

MONTHLY_UTILITIES = [
    ("2026-06", 11.0, 912.0, 145.0, 28.50, "Boiler off; net solar export credit applied to electric bill"),
    ("2026-07", 8.0, 1018.0, 130.0, 19.20, "Gas water heater load only; peak solar generation"),
    ("2026-08", 9.0, 945.0, 162.0, 24.80, "All 17 microinverters at full parity"),
    ("2026-09", 14.0, 698.0, 210.0, 42.10, "Afternoon shading dipped panel #11 output by 18%"),
    ("2026-10", 41.0, 428.0, 318.0, 89.60, "Boiler heating season started Oct 11; panel #11 shading cleared Oct 14"),
    ("2026-11", 76.0, 215.0, 442.0, 148.40, "Cold snap drove sustained Zone 1 and Zone 2 boiler cycling"),
]


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    db_path = root / "data" / "expenses.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(
            """
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                invoice_date TEXT NOT NULL,
                system TEXT NOT NULL,
                contractor TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                warranty_until TEXT NOT NULL,
                summary TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE monthly_utilities (
                month TEXT PRIMARY KEY,
                heating_therms REAL NOT NULL,
                solar_kwh REAL NOT NULL,
                grid_import_kwh REAL NOT NULL,
                net_utility_usd REAL NOT NULL,
                notes TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO invoices
                (invoice_number, invoice_date, system, contractor, amount_usd, warranty_until, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            INVOICES,
        )
        conn.executemany(
            """
            INSERT INTO monthly_utilities
                (month, heating_therms, solar_kwh, grid_import_kwh, net_utility_usd, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            MONTHLY_UTILITIES,
        )
        conn.commit()
    finally:
        conn.close()
    print(f"Wrote {db_path} ({len(INVOICES)} invoices, {len(MONTHLY_UTILITIES)} utility months)")


if __name__ == "__main__":
    main()
