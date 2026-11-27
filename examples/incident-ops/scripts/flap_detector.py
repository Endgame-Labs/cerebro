#!/usr/bin/env python3
"""Scan data/slo.sqlite and logs/deployments-and-alerts.jsonl for recurring flap
signatures and error-budget burn spikes.

Prints blank-line-separated paragraphs so Cerebro turns each paragraph into one
citable evidence item.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path


def main() -> None:
    root = Path(os.environ.get("CEREBRO_WORKSPACE") or Path(__file__).resolve().parents[1])
    db_path = root / "data" / "slo.sqlite"
    log_path = root / "logs" / "deployments-and-alerts.jsonl"

    alerts_by_service: dict[str, list[dict[str, str]]] = defaultdict(list)
    deploys_by_service: dict[str, list[dict[str, str]]] = defaultdict(list)
    if log_path.is_file():
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            if row.get("event_type") == "alert":
                alerts_by_service[row["service_slug"]].append(row)
            elif row.get("event_type") == "deployment":
                deploys_by_service[row["service_slug"]].append(row)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        budgets = list(conn.execute(
            "SELECT service, target_availability_pct, budget_consumed_pct, p99_target_ms, status "
            "FROM slo_budgets ORDER BY budget_consumed_pct DESC"
        ))

        paragraphs: list[str] = []
        for b in budgets:
            svc = b["service"]
            # Find recurring (day_of_week, hour_utc) slots with burn_rate >= 5.0 occurring >= 2 times.
            recurring = list(conn.execute(
                """
                SELECT day_of_week, hour_utc,
                       COUNT(*) AS weeks,
                       MIN(substr(ts_utc, 1, 10)) AS first_day,
                       MAX(substr(ts_utc, 1, 10)) AS last_day,
                       MIN(p99_ms) AS min_p99,
                       MAX(p99_ms) AS max_p99,
                       MIN(burn_rate) AS min_burn,
                       MAX(burn_rate) AS max_burn,
                       SUM(restarts) AS total_restarts
                FROM slo_hourly
                WHERE service = ? AND burn_rate >= 5.0
                GROUP BY day_of_week, hour_utc
                HAVING COUNT(*) >= 2
                ORDER BY weeks DESC, max_burn DESC
                """,
                (svc,),
            ))

            # Find non-recurring high-burn events (burn_rate >= 2.0 outside recurring slots).
            recurring_slots = {(r["day_of_week"], r["hour_utc"]) for r in recurring}
            all_spikes = list(conn.execute(
                """
                SELECT ts_utc, day_of_week, hour_utc, p99_ms, error_rate_pct, burn_rate, restarts
                FROM slo_hourly
                WHERE service = ? AND burn_rate >= 2.0
                ORDER BY burn_rate DESC
                """,
                (svc,),
            ))
            isolated = [s for s in all_spikes if (s["day_of_week"], s["hour_utc"]) not in recurring_slots]

            baseline = conn.execute(
                "SELECT ROUND(AVG(p99_ms), 1) AS avg_p99 FROM slo_hourly WHERE service = ? AND burn_rate < 2.0",
                (svc,),
            ).fetchone()["avg_p99"]

            svc_alerts = alerts_by_service.get(svc, [])
            alert_names = sorted({a["alert_name"] for a in svc_alerts if a.get("alert_name") and a["alert_name"] != "none"})
            shas_seen_in_alerts = sorted({a["release_sha"] for a in svc_alerts if a.get("release_sha")})

            parts = [
                f"Service {svc}: 30-day error-budget consumption is {b['budget_consumed_pct']}% "
                f"(status: {b['status']}, availability SLO {b['target_availability_pct']}%, "
                f"p99 target {b['p99_target_ms']} ms, steady-state average p99 {baseline} ms)."
            ]

            if recurring:
                for r in recurring:
                    parts.append(
                        f"Recurring time-of-week flap signature detected on {svc} every {r['day_of_week']} at "
                        f"{r['hour_utc']:02d}:00 UTC across {r['weeks']} weeks ({r['first_day']} to {r['last_day']}): "
                        f"p99 latency spiked to {r['min_p99']}-{r['max_p99']} ms, error-budget burn rate hit "
                        f"{r['min_burn']}x-{r['max_burn']}x, and pods accumulated {r['total_restarts']} total restarts."
                    )
                if alert_names:
                    parts.append(
                        f"Matching alerts in logs for {svc}: {', '.join(alert_names)} across "
                        f"{len(shas_seen_in_alerts)} release SHAs ({', '.join(shas_seen_in_alerts)})."
                    )
            else:
                parts.append(f"No recurring time-of-week flap signature detected on {svc}.")

            if isolated:
                top_spikes = "; ".join(
                    f"{s['ts_utc']} (p99 {s['p99_ms']} ms, error rate {s['error_rate_pct']}%, burn {s['burn_rate']}x, {s['restarts']} {'restart' if s['restarts'] == 1 else 'restarts'})"
                    for s in isolated[:3]
                )
                parts.append(f"Non-recurring burn spikes on {svc}: {top_spikes}.")

            paragraphs.append(" ".join(parts))

        print("\n\n".join(paragraphs))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
