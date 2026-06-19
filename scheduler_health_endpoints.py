"""
Scheduler Health Monitor — Scorr (cc_task #16, 19-Jun-2026)

GET /api/health/scheduler — exposes live-tick freshness so a stalled scheduler
is visible instead of failing silently.

Liveness signal = MAX(computed_at) from v8_metrics (the live signal writer stamps
every row with NOW() each 5-min tick). Minutes-since is computed with the DB clock
(NOW() - MAX) so it is timezone-consistent regardless of server/DB tz.

Status:
  OK    — last tick <= 7 min (during market hours)
  STALE — 7 < last tick <= 15 min
  DEAD  — last tick > 15 min
  IDLE  — outside market hours (no ticks expected)
  NO_DATA — no v8_metrics rows at all
"""

import os
import psycopg
from fastapi import APIRouter

import scheduler

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

STALE_MIN = 7
DEAD_MIN = 15


@router.get("/api/health/scheduler")
def health_scheduler():
    last_ts = None
    mins = None
    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(computed_at),
                       EXTRACT(EPOCH FROM (NOW() - MAX(computed_at)))/60.0
                FROM v8_metrics
            """)
            row = cur.fetchone()
        if row:
            last_ts = row[0].isoformat() if row[0] is not None else None
            mins = round(float(row[1]), 2) if row[1] is not None else None
    except Exception as e:
        return {"status": "ERROR", "error": str(e),
                "last_tick_ts": None, "minutes_since_last_tick": None}

    market_hours = scheduler._is_market_hours(scheduler._ist_now())

    if mins is None:
        status = "NO_DATA"
    elif not market_hours:
        status = "IDLE"
    elif mins > DEAD_MIN:
        status = "DEAD"
    elif mins > STALE_MIN:
        status = "STALE"
    else:
        status = "OK"

    return {
        "status": status,
        "last_tick_ts": last_ts,
        "minutes_since_last_tick": mins,
        "market_hours": market_hours,
        "thresholds": {"stale_min": STALE_MIN, "dead_min": DEAD_MIN},
        **scheduler.health_state(),
    }
