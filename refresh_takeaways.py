"""
refresh_takeaways.py — Scorr Content Refresh Scheduler
========================================================
NO Anthropic API key required. No autonomous AI generation.
No surprise charges. Zero credit card risk.

CONTENT STRUCTURE (locked spec 'input_raw_content_structure_v2'):

  overview        — All 1700+ companies. Annual (31 May).
                    What company does, segments, moat, promoter, geography.
                    For 501+: also includes merged legacy takeaway.

  key_takeaway    — Top 500 only (mcap_rank <= 500). Quarterly (Jan/Apr/Jul/Oct).
                    News stories, management reports, concall highlights,
                    investor presentations. NO quarterly results.

  result_analysis — Top 500 only (mcap_rank <= 500). Quarterly result seasons.
                    Triggered in last week of: Aug (Q1), Nov (Q2), Feb (Q3), May (Q4).
                    Quarterly results summary — revenue, PAT, margins,
                    beats/misses, management guidance.

RULES:
  - 501+ companies: key_takeaway = NULL always.
  - No GVM/score/verdict in any field.
  - Sources: market data, news, filings, concalls, investor presentations.

HOW IT WORKS:
  1. Railway scheduler checks due dates daily at 06:00 IST.
  2. When due → sets app_config flag + logs reminder.
  3. Daily digest surfaces: "Takeaway/Result refresh due — say 'run refresh'."
  4. Arpit triggers in Claude chat → Claude generates in-session via run_sql.
  5. Claude calls mark_refresh_complete to clear flag.
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
    """Next quarterly trigger: 1st of Jan/Apr/Jul/Oct."""
    today = date.today()
    quarterly = [date(y, m, 1) for y in [today.year, today.year+1] for m in [1,4,7,10]]
    return next(d for d in quarterly if d >= today)


def next_annual_due(month: int = 5, day: int = 31) -> date:
    today = date.today()
    candidate = date(today.year, month, day)
    return candidate if candidate >= today else date(today.year+1, month, day)


def next_result_season_due() -> date:
    """
    Next result analysis trigger: last week of Aug/Nov/Feb/May.
    Defined as the 24th of those months (7-day window through month end).
    """
    today = date.today()
    season_months = [2, 5, 8, 11]
    candidates = []
    for y in [today.year, today.year + 1]:
        for m in season_months:
            candidates.append(date(y, m, 24))
    return next(d for d in sorted(candidates) if d >= today)


def is_result_season() -> bool:
    """
    Returns True only during result analysis refresh windows:
    last week of Aug (Q1), Nov (Q2), Feb (Q3), May (Q4).
    Window = 24th through last day of month.
    """
    today = date.today()
    if today.month not in (2, 5, 8, 11):
        return False
    return today.day >= 24


def is_due(last_updated: Optional[date], cadence: str) -> bool:
    """
    For 'quarterly' (takeaway + overview): use rolling day threshold.
    For 'result_analysis': use explicit season window — DO NOT use rolling days.
    Use is_result_season() directly for result_analysis checks.
    """
    if last_updated is None:
        return True
    days = (date.today() - last_updated).days
    return days >= 85 if cadence == "quarterly" else days >= 350


# ── app_config helpers ────────────────────────────────────────────────────────

def _set_config(key: str, value: str):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (key, value)); conn.commit()


def _get_config(key: str, default: str = "") -> str:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            r = cur.fetchone(); return r[0] if r else default
    except: return default


# ── Reminder logger ───────────────────────────────────────────────────────────

def _log_reminder(schedule: str, next_due: date, days_overdue: int):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_log (category, title, details, session_ts)
                VALUES (%s, %s, %s::jsonb, NOW())
            """, ("refresh_reminder", f"refresh_due_{schedule}", json.dumps({
                "schedule": schedule, "next_due": str(next_due),
                "days_overdue": days_overdue, "flagged_at": str(date.today()),
                "action": "Say 'run refresh' in Claude chat",
            }))); conn.commit()
        log.info(f"Reminder logged: {schedule} overdue by {days_overdue}d")
    except Exception as e:
        log.warning(f"_log_reminder failed: {e}")


# ── Main scheduler check (06:00 IST daily) ───────────────────────────────────

def check_and_flag_due_refreshes() -> Dict:
    """
    Checks all 3 schedules. If due → sets app_config flag + logs reminder.
    NO AI generation. NO API calls. Just flags.

    result_analysis uses season window (last week of Aug/Nov/Feb/May),
    NOT a rolling 85-day check.
    """
    today = date.today(); flagged = []

    try:
        with _conn() as conn, conn.cursor() as cur:

            # Schedule 1: key_takeaway — top 500, quarterly rolling
            cur.execute("SELECT MIN(last_takeaway_updated) FROM input_raw WHERE mcap_rank <= 500")
            oldest_t = cur.fetchone()[0]
            if is_due(oldest_t, "quarterly"):
                days_over = max(0, (today - oldest_t).days - 85) if oldest_t else 999
                _set_config("takeaway_refresh_due", "true")
                _set_config("takeaway_refresh_tier", "top500")
                _log_reminder("takeaway_top500", next_quarterly_due(), days_over)
                flagged.append("takeaway_top500")

            # Schedule 2: result_analysis — top 500, season window only
            # Only fires in last week of Aug / Nov / Feb / May
            if is_result_season():
                cur.execute("SELECT MIN(last_result_analysis_updated) FROM input_raw WHERE mcap_rank <= 500")
                oldest_r = cur.fetchone()[0]
                # Only flag if not already updated this season
                # (i.e. last update is before the 24th of current month)
                season_start = date(today.year, today.month, 24)
                already_done = oldest_r and oldest_r >= season_start
                if not already_done:
                    _set_config("result_analysis_refresh_due", "true")
                    _log_reminder("result_analysis_top500", next_result_season_due(), 0)
                    flagged.append("result_analysis_top500")
            else:
                # Outside season window — always clear the flag
                _set_config("result_analysis_refresh_due", "false")

            # Schedule 3: overview — all companies, annual
            cur.execute("SELECT MIN(last_overview_updated) FROM input_raw")
            oldest_ov = cur.fetchone()[0]
            if is_due(oldest_ov, "annual"):
                days_over = max(0, (today - oldest_ov).days - 350) if oldest_ov else 999
                _set_config("overview_refresh_due", "true")
                _log_reminder("overview_all", next_annual_due(), days_over)
                flagged.append("overview_all")

    except Exception as e:
        log.error(f"check_and_flag_due_refreshes failed: {e}")
        return {"status": "error", "error": str(e)}

    return {
        "status": "ok", "checked_at": str(today), "flagged": flagged,
        "takeaway_refresh_due": _get_config("takeaway_refresh_due", "false"),
        "result_analysis_refresh_due": _get_config("result_analysis_refresh_due", "false"),
        "overview_refresh_due": _get_config("overview_refresh_due", "false"),
        "next_quarterly_due": str(next_quarterly_due()),
        "next_annual_due": str(next_annual_due()),
        "next_result_season_due": str(next_result_season_due()),
        "in_result_season": is_result_season(),
    }


# ── Status reader (digest + MCP) ─────────────────────────────────────────────

def get_refresh_status() -> Dict:
    today = date.today()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT MIN(last_takeaway_updated), COUNT(*) FROM input_raw WHERE mcap_rank <= 500")
            r = cur.fetchone(); t_min, t_count = r

            cur.execute("SELECT MIN(last_result_analysis_updated), COUNT(*) FROM input_raw WHERE mcap_rank <= 500")
            r = cur.fetchone(); ra_min, ra_count = r

            cur.execute("SELECT MIN(last_overview_updated), COUNT(*) FROM input_raw")
            r = cur.fetchone(); ov_min, ov_count = r

            cur.execute("""
                SELECT title, session_ts FROM session_log
                WHERE category = 'refresh_reminder' ORDER BY session_ts DESC LIMIT 5
            """)
            last_reminders = [{"title": r[0], "ts": str(r[1])} for r in cur.fetchall()]

        def days_ago(d): return (today - d).days if d else None

        return {
            "today": str(today),
            "flags": {
                "takeaway_refresh_due": _get_config("takeaway_refresh_due", "false"),
                "takeaway_refresh_tier": _get_config("takeaway_refresh_tier", ""),
                "result_analysis_refresh_due": _get_config("result_analysis_refresh_due", "false"),
                "overview_refresh_due": _get_config("overview_refresh_due", "false"),
            },
            "schedules": {
                "key_takeaway_top500": {
                    "scope": "mcap_rank <= 500",
                    "content": "News + concall highlights + management reports (NO results)",
                    "count": t_count,
                    "oldest_update": str(t_min) if t_min else None,
                    "days_since_oldest": days_ago(t_min),
                    "cadence": "quarterly",
                    "next_due": str(next_quarterly_due()),
                    "overdue": is_due(t_min, "quarterly"),
                    "how_to_run": "Say 'run takeaway refresh' in Claude chat",
                },
                "result_analysis_top500": {
                    "scope": "mcap_rank <= 500",
                    "content": "Quarterly results — revenue, PAT, margins, guidance, beats/misses",
                    "count": ra_count,
                    "oldest_update": str(ra_min) if ra_min else None,
                    "days_since_oldest": days_ago(ra_min),
                    "cadence": "quarterly",
                    "next_due": str(next_result_season_due()),
                    "in_season": is_result_season(),
                    "overdue": is_result_season() and (
                        ra_min is None or ra_min < date(today.year, today.month, 24)
                    ),
                    "how_to_run": "Say 'run result analysis refresh' in Claude chat",
                },
                "overview_all": {
                    "scope": "All 1700+ (501+ includes merged takeaway)",
                    "content": "What company does, segments, moat, promoter, geography",
                    "count": ov_count,
                    "oldest_update": str(ov_min) if ov_min else None,
                    "cadence": "annual",
                    "next_due": str(next_annual_due()),
                    "overdue": is_due(ov_min, "annual"),
                    "how_to_run": "Say 'run overview refresh' in Claude chat",
                },
            },
            "last_reminders": last_reminders,
            "note": "No API key. Claude generates in-session on manual trigger.",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Mark complete ─────────────────────────────────────────────────────────────

def mark_refresh_complete(field: str, tier: str, count: int) -> Dict:
    """Called after Claude finishes in-session generation. Clears due flag."""
    try:
        config_map = {
            "takeaway": "takeaway_refresh_due",
            "result_analysis": "result_analysis_refresh_due",
            "overview": "overview_refresh_due",
        }
        if field in config_map:
            _set_config(config_map[field], "false")
        if field == "takeaway":
            _set_config("takeaway_refresh_tier", "")

        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_log (category, title, details, session_ts)
                VALUES (%s, %s, %s::jsonb, NOW())
            """, ("refresh_run", f"{field}_refresh_{tier}_complete", json.dumps({
                "field": field, "tier": tier,
                "run_date": str(date.today()),
                "companies_updated": count,
                "method": "claude_in_session",
            }))); conn.commit()

        return {"status": "ok", "field": field, "tier": tier, "companies_updated": count}
    except Exception as e:
        log.error(f"mark_refresh_complete failed: {e}")
        return {"status": "error", "error": str(e)}
