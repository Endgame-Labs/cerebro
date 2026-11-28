#!/usr/bin/env python3
"""Build data/slo.sqlite with deterministic hourly SLO burn and latency data."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "slo.sqlite"

SERVICES = {
    "checkout-worker": {
        "target_availability_pct": 99.95,
        "budget_consumed_pct": 74.2,
        "p99_target_ms": 250.0,
        "status": "warning",
        "base_p99": 142.0,
        "base_err": 0.02,
        "base_burn": 0.45,
    },
    "webhook-dispatcher": {
        "target_availability_pct": 99.90,
        "budget_consumed_pct": 118.4,
        "p99_target_ms": 500.0,
        "status": "breached",
        "base_p99": 215.0,
        "base_err": 0.05,
        "base_burn": 0.60,
    },
    "ledger-api": {
        "target_availability_pct": 99.99,
        "budget_consumed_pct": 19.2,
        "p99_target_ms": 120.0,
        "status": "healthy",
        "base_p99": 74.0,
        "base_err": 0.004,
        "base_burn": 0.20,
    },
}

# Specific anomalies injected into the 30-day hourly series (2026-10-28 .. 2026-11-26).
TUESDAY_SPIKES = {
    "2026-11-03": (2340.0, 1.85, 10.6, 8),
    "2026-11-10": (2290.0, 1.72, 9.9, 6),
    "2026-11-17": (2480.0, 2.10, 11.8, 9),
    "2026-11-24": (2180.0, 1.64, 9.4, 6),
}


def build() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(
            """
            CREATE TABLE slo_budgets (
                service TEXT PRIMARY KEY,
                target_availability_pct REAL NOT NULL,
                budget_consumed_pct REAL NOT NULL,
                p99_target_ms REAL NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE slo_hourly (
                service TEXT NOT NULL,
                ts_utc TEXT NOT NULL,
                day_of_week TEXT NOT NULL,
                hour_utc INTEGER NOT NULL,
                p99_ms REAL NOT NULL,
                error_rate_pct REAL NOT NULL,
                burn_rate REAL NOT NULL,
                restarts INTEGER NOT NULL,
                PRIMARY KEY (service, ts_utc)
            );
            """
        )

        for service, cfg in SERVICES.items():
            conn.execute(
                """
                INSERT INTO slo_budgets
                    (service, target_availability_pct, budget_consumed_pct, p99_target_ms, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    service,
                    cfg["target_availability_pct"],
                    cfg["budget_consumed_pct"],
                    cfg["p99_target_ms"],
                    cfg["status"],
                    "2026-11-26T23:00:00Z",
                ),
            )

        start = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        end = datetime(2026, 11, 27, 0, 0, tzinfo=UTC)
        rows = []
        current = start
        step = 0
        while current < end:
            day_str = current.strftime("%Y-%m-%d")
            dow = current.strftime("%A")
            hour = current.hour
            ts = current.strftime("%Y-%m-%dT%H:%M:%SZ")
            jitter = ((step * 7) % 11) - 5  # deterministic small variation

            for service, cfg in SERVICES.items():
                p99 = round(cfg["base_p99"] + jitter * 1.4, 1)
                err = round(cfg["base_err"] + max(0, jitter) * 0.002, 4)
                burn = round(cfg["base_burn"] + max(0, jitter) * 0.03, 2)
                restarts = 0

                # Recurring Tuesday 03:00 UTC spike on checkout-worker
                if service == "checkout-worker" and day_str in TUESDAY_SPIKES and hour == 3:
                    p99, err, burn, restarts = TUESDAY_SPIKES[day_str]

                # INC-204: Redis Failover Storm on 2026-11-12 14:00-15:00 UTC
                if day_str == "2026-11-12" and hour == 14:
                    if service == "webhook-dispatcher":
                        p99, err, burn, restarts = 4820.0, 14.2, 28.5, 4
                    elif service == "checkout-worker":
                        p99, err, burn, restarts = 1920.0, 1.45, 7.2, 2
                    elif service == "ledger-api":
                        p99, err, burn, restarts = 310.0, 0.18, 2.1, 0
                elif day_str == "2026-11-12" and hour == 15 and service == "webhook-dispatcher":
                    p99, err, burn, restarts = 1640.0, 4.1, 9.8, 1

                # INC-211: Webhook Timeout Cascade on 2026-11-21 18:00 UTC
                if day_str == "2026-11-21" and hour == 18 and service == "webhook-dispatcher":
                    p99, err, burn, restarts = 11800.0, 8.9, 16.4, 3

                rows.append((service, ts, dow, hour, p99, err, burn, restarts))

            current += timedelta(hours=1)
            step += 1

        conn.executemany(
            """
            INSERT INTO slo_hourly
                (service, ts_utc, day_of_week, hour_utc, p99_ms, error_rate_pct, burn_rate, restarts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    build()
    print(f"wrote {DB_PATH}")
