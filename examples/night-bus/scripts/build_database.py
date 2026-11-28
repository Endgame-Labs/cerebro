"""Rebuild the fictional five-night service sample with Python's standard library."""
from pathlib import Path
import csv
import sqlite3

root = Path(__file__).resolve().parents[1]
path = root / "data/service.sqlite"
path.unlink(missing_ok=True)
with sqlite3.connect(path) as db:
    for table, key in [("routes", "route_id"), ("stops", "stop_id")]:
        with (root / f"data/{table}.csv").open() as source:
            rows = list(csv.DictReader(source))
        columns = list(rows[0])
        db.execute(f"CREATE TABLE {table} ({', '.join(c + ' TEXT' for c in columns)}, PRIMARY KEY ({key}))")
        db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", [list(r.values()) for r in rows])
    db.execute("CREATE TABLE trips (trip_id TEXT PRIMARY KEY, route_id TEXT, service_date TEXT, stop_id TEXT, scheduled_min INTEGER)")
    db.execute("CREATE TABLE arrivals (trip_id TEXT PRIMARY KEY, actual_min INTEGER, departure_min INTEGER)")
    for day in range(16, 21):
        for route, count, stop, base, delays in [
            ("R8", 10, "LS-W", 1200, [0, 1, 2, 0, 3, 1, 0, 2, 0, 1]),
            ("R4", 6, "LS-E", 1230, [0, 0, 1, 0, 0, 0]),
            ("R12", 4, "LS-E", 1238, [2, 3, 2, [4, 5, 4, 1, 6][day - 16]]),
        ]:
            for i in range(count):
                trip = f"{route}-{day}-{i}"
                scheduled = base + i * 20 if i < count - 1 else {"R8": 1340, "R4": 1330, "R12": 1328}[route]
                db.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?)", (trip, route, f"2026-11-{day}", stop, scheduled))
                db.execute("INSERT INTO arrivals VALUES (?, ?, ?)", (trip, scheduled + delays[i], scheduled + delays[i] if route == "R4" else None))
print("Built 3 routes, 5 stops, 100 trips, and 100 arrivals.")
