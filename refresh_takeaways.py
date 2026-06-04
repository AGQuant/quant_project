"""
refresh_takeaways.py — Scorr Content Refresh Scheduler
========================================================
NO Anthropic API key required. No autonomous AI generation.
No surprise charges. Zero credit card risk.

HOW IT WORKS:
  1. Railway scheduler checks due dates daily at 06:00 IST.
  2. When due → sets app_config flag + logs reminder to session_log.
  3. Daily digest surfaces the flag to Arpit.
  4. Arpit says "run refresh" in Claude chat.
  5. Claude generates takeaways/overviews in-session using Railway data.
  6. Claude writes results back via run_sql MCP tool.
  7. Updates last_takeaway_updated / last_overview_updated per company.

4 SCHEDULES (locked in session_log spec 'input_raw_refresh_schedule_v2'):
  1. Takeaway — Top 500 (mcap_rank <= 500)  : Quarterly  (1 Jan / 1 Apr / 1 Jul / 1 Oct)
  2. Takeaway — Rank 501-1700               : Annual     (31 May)
  3. Overview  — All 1700+                  : Annual     (31 May)
  4. Revenue estimates (FY27→FY28)          : Annual     (31 May)

CONTENT RULES (locked in session_log 'overview_takeaway_content_rules'):
  - Sources: market data, news, filings, earnings calls, analyst consensus
  - EXCLUDED: GVM / G / V / M scores, verdict, punchline, any Scorr model reference
  - Takeaway: 500-800 chars, factual, data-driven, forward-looking
  - Overview:  400-600 chars, factual, what company does, segments, moat
"""

import os
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Dict

import psycopg

log = logging.getLogger("scorr.refresh_takeaways")

DATABASE_URL = os.getenv("DATABASE_URL")


def _conn():
    return psycopg.connect(DATABASE_URL)


# ── Due date calculators ──────────────────────────────────────────────────────

def next_quarterly_due() -> date:
    """Next quarterly trigger date: 1st of Jan/Apr/Jul/Oct."""
    today = date.today()
    quarterly = []
    for year in [today.year, today.year + 1]:
        for month in [1, 4, 7, 10]:
            quarterly.append(date(year, month, 1))
    future = [d for d in quarterly if d >= today]
    return future[0]


def next_annual_due(anchor_month: int = 5, anchor_day: int = 31) -> date:
    """Next annual trigger date: 31 May each year."""
    today = date.today()
    candidate = date(today.year, anchor_month, anchor_day)
    if candidate < today:
        candidate = date(today.year + 1, anchor_month, anchor_day)
    return candidate


def is_due(last_updated: Optional[date], cadence: str) -> bool:
    """True if refresh is overdue."""
    if last_updated is None:
        return True
    today = date.today()
    if cadence == "quarterly":
        return (today - last_updated).days >= 85
    elif cadence == "annual":
        return (today - last_updated).days >= 350
    return False


# ── app_config helpers ────────────────────────────────────────────────────────

def _set_config(key: str, value: str):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO app_config (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (key, value))
        conn.commit()


def _get_config(key: str, default: str = "") -> str:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            r = cur.fetchone()
            return r[0] if r else default
    except Exception:
        return default


# ── Reminder logger ───────────────────────────────────────────────────────────

def _log_reminder(schedule: str, next_due: date, days_overdue: int):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_log (category, title, details, session_ts)
                VALUES (%s, %s, %s::jsonb, NOW())
            """, (
                "refresh_reminder",
                f"refresh_due_{schedule}",
                json.dumps({
                    "schedule": schedule,
                    "next_due": str(next_due),
                    "days_overdue": days_overdue,
                    "action": "Say 'run takeaway refresh' or 'run overview refresh' in Claude chat",
                    "flagged_at": str(date.today()),
                }),
            ))
            conn.commit()
        log.info(f"Reminder logged: {schedule} overdue by {days_overdue}d")
    except Exception as e:
        log.warning(f"_log_reminder failed: {e}")


# ── Main scheduler check (runs daily at 06:00 IST) ───────────────────────────

def check_and_flag_due_refreshes() -> Dict:
    """
    Called by the Railway scheduler at 06:00 IST daily.
    Checks all 4 schedules. If due → sets app_config flag + logs reminder.
    NO AI generation. NO API calls. Just flags.
    """
    today = date.today()
    flagged = []

    try:
        with _conn() as conn, conn.cursor() as cur:

            # Schedule 1: Takeaway top 500 — quarterly
            cur.execute("""
                SELECT MIN(last_takeaway_updated)
                FROM input_raw WHERE mcap_rank <= 500
            """)
            r = cur.fetchone()
            oldest_t500 = r[0] if r else None
            if is_due(oldest_t500, "quarterly"):
                days_over = (today - oldest_t500).days - 85 if oldest_t500 else 999
                _set_config("takeaway_refresh_due", "true")
                _set_config("takeaway_refresh_tier", "top500")
                _log_reminder("takeaway_top500", next_quarterly_due(), max(0, days_over))
                flagged.append("takeaway_top500")

            # Schedule 2: Takeaway 501-1700 — annual
            cur.execute("""
                SELECT MIN(last_takeaway_updated)
                FROM input_raw WHERE mcap_rank BETWEEN 501 AND 1700
            """)
            r = cur.fetchone()
            oldest_mid = r[0] if r else None
            if is_due(oldest_mid, "annual"):
                days_over = (today - oldest_mid).days - 350 if oldest_mid else 999
                _set_config("takeaway_refresh_due", "true")
                _set_config("takeaway_refresh_tier", "mid")
                _log_reminder("takeaway_501_1700", next_annual_due(), max(0, days_over))
                flagged.append("takeaway_501_1700")

            # Schedule 3: Overview — annual
            cur.execute("SELECT MIN(last_overview_updated) FROM input_raw")
            r = cur.fetchone()
            oldest_ov = r[0] if r else None
            if is_due(oldest_ov, "annual"):
                days_over = (today - oldest_ov).days - 350 if oldest_ov else 999
                _set_config("overview_refresh_due", "true")
                _log_reminder("overview_all_1700", next_annual_due(), max(0, days_over))
                flagged.append("overview_all_1700")

    except Exception as e:
        log.error(f"check_and_flag_due_refreshes failed: {e}")
        return {"status": "error", "error": str(e)}

    return {
        "status": "ok",
        "checked_at": str(today),
        "flagged": flagged,
        "takeaway_refresh_due": _get_config("takeaway_refresh_due", "false"),
        "overview_refresh_due": _get_config("overview_refresh_due", "false"),
        "next_quarterly_due": str(next_quarterly_due()),
        "next_annual_due": str(next_annual_due()),
    }


# ── Status reader (for digest + MCP) ─────────────────────────────────────────

def get_refresh_status() -> Dict:
    """Returns current refresh status and next due dates for all 4 schedules."""
    today = date.today()

    try:
        with _conn() as conn, conn.cursor() as cur:

            cur.execute("""
                SELECT MIN(last_takeaway_updated), MAX(last_takeaway_updated), COUNT(*)
                FROM input_raw WHERE mcap_rank <= 500
            """)
            r = cur.fetchone()
            t500_min, t500_max, t500_count = r

            cur.execute("""
                SELECT MIN(last_takeaway_updated), MAX(last_takeaway_updated), COUNT(*)
                FROM input_raw WHERE mcap_rank BETWEEN 501 AND 1700
            """)
            r = cur.fetchone()
            t_mid_min, t_mid_max, t_mid_count = r

            cur.execute("""
                SELECT MIN(last_overview_updated), MAX(last_overview_updated), COUNT(*)
                FROM input_raw
            """)
            r = cur.fetchone()
            ov_min, ov_max, ov_count = r

            cur.execute("""
                SELECT title, details::text, session_ts
                FROM session_log
                WHERE category = 'refresh_reminder'
                ORDER BY session_ts DESC LIMIT 5
            """)
            last_reminders = [{"title": r[0], "ts": str(r[2])} for r in cur.fetchall()]

        def days_ago(d):
            return (today - d).days if d else None

        return {
            "today": str(today),
            "flags": {
                "takeaway_refresh_due": _get_config("takeaway_refresh_due", "false"),
                "takeaway_refresh_tier": _get_config("takeaway_refresh_tier", ""),
                "overview_refresh_due": _get_config("overview_refresh_due", "false"),
            },
            "schedules": {
                "takeaway_top500": {
                    "scope": "mcap_rank <= 500",
                    "count": t500_count,
                    "oldest_update": str(t500_min) if t500_min else None,
                    "days_since_oldest": days_ago(t500_min),
                    "cadence": "quarterly",
                    "next_due": str(next_quarterly_due()),
                    "overdue": is_due(t500_min, "quarterly"),
                    "how_to_run": "Say 'run takeaway refresh top500' in Claude chat",
                },
                "takeaway_501_1700": {
                    "scope": "mcap_rank 501-1700",
                    "count": t_mid_count,
                    "oldest_update": str(t_mid_min) if t_mid_min else None,
                    "cadence": "annual",
                    "next_due": str(next_annual_due()),
                    "overdue": is_due(t_mid_min, "annual"),
                    "how_to_run": "Say 'run takeaway refresh mid' in Claude chat",
                },
                "overview_all": {
                    "scope": "all 1700+",
                    "count": ov_count,
                    "oldest_update": str(ov_min) if ov_min else None,
                    "cadence": "annual",
                    "next_due": str(next_annual_due()),
                    "overdue": is_due(ov_min, "annual"),
                    "how_to_run": "Say 'run overview refresh' in Claude chat",
                },
            },
            "last_reminders": last_reminders,
            "note": "No API key required. Claude generates in-session on manual trigger.",
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Mark complete (called by Claude after in-session generation) ──────────────

def mark_refresh_complete(field: str, tier: str, count: int) -> Dict:
    """
    Called after Claude finishes in-session generation and writes to DB.
    Clears the due flag and logs the completion.
    """
    try:
        today = date.today()

        if field == "takeaway":
            _set_config("takeaway_refresh_due", "false")
            _set_config("takeaway_refresh_tier", "")
        elif field == "overview":
            _set_config("overview_refresh_due", "false")

        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_log (category, title, details, session_ts)
                VALUES (%s, %s, %s::jsonb, NOW())
            """, (
                "refresh_run",
                f"{field}_refresh_{tier}_complete",
                json.dumps({
                    "field": field,
                    "tier": tier,
                    "run_date": str(today),
                    "companies_updated": count,
                    "method": "claude_in_session",
                }),
            ))
            conn.commit()

        return {"status": "ok", "field": field, "tier": tier, "companies_updated": count}

    except Exception as e:
        log.error(f"mark_refresh_complete failed: {e}")
        return {"status": "error", "error": str(e)}
