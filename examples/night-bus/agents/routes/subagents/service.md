---
evidence:
  - facts: { window: last_365_days }
  - sql:
      source: service
      query: >-
        SELECT 'route sample' AS measurement, t.route_id,
        count(*) AS observed_arrivals,
        round(avg(a.actual_min-t.scheduled_min),2) AS mean_delay_minutes,
        max(a.actual_min-t.scheduled_min) AS max_delay_minutes,
        NULL AS service_date, NULL AS r12_arrival, NULL AS r4_departure,
        NULL AS missed_by_timing, NULL AS stop_id
        FROM trips t JOIN arrivals a USING(trip_id) GROUP BY t.route_id
        UNION ALL
        SELECT 'last transfer', 'R12 to R4', NULL, NULL, NULL, i.service_date,
        printf('%02d:%02d', ai.actual_min/60, ai.actual_min%60),
        printf('%02d:%02d', ao.departure_min/60, ao.departure_min%60),
        CASE WHEN ai.actual_min>ao.departure_min THEN 1 ELSE 0 END, 'LS-E'
        FROM trips i JOIN arrivals ai ON ai.trip_id=i.trip_id
        JOIN trips o ON o.service_date=i.service_date AND o.route_id='R4' AND o.scheduled_min=1330
        JOIN arrivals ao ON ao.trip_id=o.trip_id
        WHERE i.route_id='R12' AND i.scheduled_min=1328
heartbeat: [change]
---

# Instructions

Quantify this route's five-night sample, 16 to 20 November 2026, citing SQL rows.
Observed arrival count is service volume, not passenger volume. Compare routes
where useful. Explain the last transfer timing at LS-E only where this route
serves it. The transfer rows give local clock times.
Do not extrapolate a five-night sample to all service.
