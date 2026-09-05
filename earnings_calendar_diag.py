"""
earnings_calendar_diag.py — cc#1707 scope 5 BACKSTOP CHECK (read-only).
==========================================================================
Counts earnings_calendar rows that claim a result without a confirmed source, so the phantom
population cannot silently rebuild after the cc#1707 quarantine:

  phantom_reported : verified='false' AND status='reported'   -> must be 0 (the cc#1707 defect)
  phantom_upcoming : verified='false' AND status='upcoming'   -> must be 0 (pre-P2 leads that
                     would age into 'reported' on any date-only flip)
  leads            : status='lead'                            -> informational (the P2 writer's
                     honest home for a news-discovered lead)

Surfaces: (a) a check in /api/health/report section data_feeds ("Earnings calendar phantoms"),
(b) GET /api/diag/earnings_calendar with the counts and the offending tickers. Nothing here writes.
"""
import logging
import os
from fastapi import APIRouter

log = logging.getLogger("earnings_calendar_diag")
router = APIRouter()

_SQL = """
    SELECT
      COUNT(*) FILTER (WHERE verified::text = 'false' AND status = 'reported') AS phantom_reported,
      COUNT(*) FILTER (WHERE verified::text = 'false' AND status = 'upcoming') AS phantom_upcoming,
      COUNT(*) FILTER (WHERE status = 'lead') AS leads,
      COALESCE(array_agg(UPPER(ticker) || ' ' || ex_date::text ORDER BY ex_date DESC)
               FILTER (WHERE verified::text = 'false' AND status IN ('reported', 'upcoming')), '{}') AS offenders
    FROM earnings_calendar
"""


def phantom_counts(cur) -> dict:
    """Read-only. Returns {phantom_reported, phantom_upcoming, leads, offenders[]}."""
    cur.execute(_SQL)
    r = cur.fetchone()
    return {"phantom_reported": int(r[0] or 0), "phantom_upcoming": int(r[1] or 0),
            "leads": int(r[2] or 0), "offenders": list(r[3] or [])}


def health_check(cur) -> dict:
    """One check dict for main.build_health_report's add_check(): ok when both phantom counts are 0,
    warn when only unaged 'upcoming' leads exist, fail when a verified='false' row reads 'reported'."""
    try:
        c = phantom_counts(cur)
        value = (f"{c['phantom_reported']} reported/unverified, {c['phantom_upcoming']} upcoming/unverified, "
                 f"{c['leads']} leads")
        if c["phantom_reported"] > 0:
            status = "fail"
        elif c["phantom_upcoming"] > 0:
            status = "warn"
        else:
            status = "ok"
        return {"check": "Earnings calendar phantoms", "value": value, "status": status}
    except Exception as e:
        log.warning(f"earnings_calendar phantom check: {e}")
        return {"check": "Earnings calendar phantoms", "value": str(e)[:160], "status": "fail"}


@router.get("/api/diag/earnings_calendar")
def diag_earnings_calendar():
    """Read-only backstop: phantom counts + offending (ticker, ex_date) list. cc#1707 scope 5."""
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        c = phantom_counts(cur)
    c["ok"] = c["phantom_reported"] == 0 and c["phantom_upcoming"] == 0
    c["rule"] = "verified='false' rows are cc#602 news leads and must never carry status='reported' (cc#1707)"
    return c
