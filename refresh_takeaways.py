"""
refresh_takeaways.py — Scorr AI Takeaway Generator
====================================================
Generates key_takeaway (and optionally overview) for input_raw using Claude API.
Data sources: screener_raw (financials) + company overview (context).
NO reference to GVM, scores, verdicts or any Scorr internal model.

Schedules (locked in session_log spec 'input_raw_refresh_schedule_v2'):
  1. Takeaway — Top 500 (mcap_rank <= 500)  : Quarterly  (31 Mar / 30 Jun / 30 Sep / 31 Dec)
  2. Takeaway — Rank 501-1700               : Annual     (31 May)
  3. Overview — All 1700+                   : Annual     (31 May)

Content rules (locked in session_log spec 'overview_takeaway_content_rules'):
  - Sources: market data, news, filings, earnings calls, analyst consensus
  - Excluded: GVM / G / V / M scores, verdict, punchline, any Scorr model reference
  - Takeaway: 500-800 chars, factual, data-driven, forward-looking
  - Overview: 400-600 chars, factual, what company does, segments, moat
"""

import os
import json
import time
import logging
import asyncio
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict

import psycopg
import httpx

log = logging.getLogger("scorr.refresh_takeaways")

DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Rate limiting — 1 call/sec to stay within API limits
RATE_SLEEP = 1.1
# Batch size for DB writes
BATCH_SIZE = 50


def _conn():
    return psycopg.connect(DATABASE_URL)


# ── Next due date calculator ──────────────────────────────────────────────────

def next_quarterly_due(anchor: date = date(2026, 3, 31)) -> date:
    """Returns next quarterly due date from anchor (31 Mar base)."""
    today = date.today()
    # Quarterly dates: Mar 31, Jun 30, Sep 30, Dec 31
    quarterly = []
    for year in [today.year, today.year + 1]:
        quarterly += [
            date(year, 3, 31),
            date(year, 6, 30),
            date(year, 9, 30),
            date(year, 12, 31),
        ]
    future = [d for d in quarterly if d >= today]
    return future[0] if future else date(today.year + 1, 3, 31)


def next_annual_due(anchor: date = date(2026, 5, 31)) -> date:
    """Returns next annual due date."""
    today = date.today()
    candidate = anchor.replace(year=today.year)
    if candidate < today:
        candidate = anchor.replace(year=today.year + 1)
    return candidate


def is_due(last_updated: Optional[date], cadence: str) -> bool:
    """Check if a refresh is due based on last update date and cadence."""
    if last_updated is None:
        return True
    today = date.today()
    if cadence == "quarterly":
        return (today - last_updated).days >= 85  # ~3 months
    elif cadence == "annual":
        return (today - last_updated).days >= 350  # ~12 months
    return False


# ── Claude API caller ─────────────────────────────────────────────────────────

async def _call_claude(prompt: str, max_tokens: int = 600) -> Optional[str]:
    """Single Claude API call. Returns text or None on failure."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": max_tokens,
                    "system": (
                        "You are a senior Indian equity research analyst writing institutional-grade "
                        "company intelligence. Be factual, data-driven, and concise. "
                        "Use only market data, public filings, news, and financial metrics provided. "
                        "Never mention GVM, scoring models, ratings systems, or investment advice. "
                        "Never use words like 'Strong Buy', 'Buy', 'Watch', 'Exit', 'Avoid'. "
                        "Write in plain English. No markdown, no bullet points, no headers. "
                        "Continuous prose only."
                    ),
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("content", [])
            for block in content:
                if block.get("type") == "text":
                    return block["text"].strip()
        return None
    except Exception as e:
        log.warning(f"Claude API error: {e}")
        return None


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_takeaway_prompt(row: dict) -> str:
    def _f(v, suffix="", decimals=1):
        try:
            return f"{round(float(v), decimals)}{suffix}" if v is not None else "N/A"
        except Exception:
            return "N/A"

    return f"""Write a key takeaway for {row['company_name']} ({row['nse_code']}) — {row['industry_group']}.

Financial data (latest available):
- Revenue growth 5Y/3Y: {_f(row.get('sales_growth_5y'), '%')} / {_f(row.get('sales_growth_3y'), '%')}
- Profit growth 5Y/3Y: {_f(row.get('profit_growth_5y'), '%')} / {_f(row.get('profit_growth_3y'), '%')}
- QoQ revenue growth: {_f(row.get('qoq_sales_growth'), '%')}
- QoQ profit growth: {_f(row.get('qoq_profit_growth'), '%')}
- OPM: {_f(row.get('opm'), '%')}
- ROCE: {_f(row.get('roce'), '%')}
- ROE: {_f(row.get('roe'), '%')}
- Debt/Equity: {_f(row.get('de'), 'x')}
- Interest coverage: {_f(row.get('interest_coverage'), 'x')}
- PE vs Historical PE: {_f(row.get('pe'), 'x')} vs {_f(row.get('historical_pe'), 'x')}
- Dividend yield: {_f(row.get('dividend_yield'), '%')}
- Promoter holding: {_f(row.get('promoter_holding'), '%')}
- FII change: {_f(row.get('fii_change'), '%')}
- DII change: {_f(row.get('dii_change'), '%')}
- 1Y / 3Y / 1M return: {_f(row.get('return_1y'), '%')} / {_f(row.get('return_3y'), '%')} / {_f(row.get('return_1m'), '%')}
- Market cap: Rs {_f(row.get('market_cap'), ' Cr', 0)}

Company overview for context (do not repeat verbatim):
{row.get('overview', 'Not available')}

Write 500-800 characters covering: latest quarterly performance, key financial trends, \
management priorities, FY27 outlook, and main risks. \
Use numbers from the data above. Be specific. No opinions, no ratings, no model references."""


def _build_overview_prompt(row: dict) -> str:
    return f"""Write a company overview for {row['company_name']} ({row['nse_code']}) — {row['industry_group']}, {row['industry']}.

Market cap: Rs {row.get('market_cap', 'N/A')} Cr. Promoter holding: {row.get('promoter_holding', 'N/A')}%.

Write 400-600 characters covering: what the company does, main business segments with \
approximate revenue mix, key competitive moat, promoter/ownership structure, and geographic presence. \
Factual only. No opinions, no ratings, no scoring model references."""


# ── Core refresh functions ────────────────────────────────────────────────────

async def refresh_takeaways(
    tier: str = "top500",
    force: bool = False,
    limit: Optional[int] = None,
) -> Dict:
    """
    tier: 'top500' (mcap_rank <= 500, quarterly) or 'mid' (501-1700, annual)
    force: ignore last_takeaway_updated check
    limit: max companies to process (for testing)
    """
    today = date.today()

    # Build query
    if tier == "top500":
        rank_filter = "i.mcap_rank <= 500"
        cadence = "quarterly"
    else:
        rank_filter = "i.mcap_rank BETWEEN 501 AND 1700"
        cadence = "annual"

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                i.nse_code, i.company_name, i.mcap_rank, i.overview,
                i.last_takeaway_updated,
                s.market_cap, s.pe, s.historical_pe,
                s.sales_growth_5y, s.profit_growth_5y,
                s.sales_growth_3y, s.profit_growth_3y,
                s.qoq_sales_growth, s.qoq_profit_growth,
                s.opm, s.roce,
                s."Return on equity" AS roe,
                s."Promoter holding" AS promoter_holding,
                s.fii_change, s.dii_change,
                s.return_1y, s.return_3y,
                s."Return over 1month" AS return_1m,
                s."Debt to equity" AS de,
                s.interest_coverage, s.dividend_yield,
                s.industry_group, s."Industry" AS industry
            FROM input_raw i
            JOIN screener_raw s ON i.nse_code = s.nse_code
            WHERE {rank_filter}
            ORDER BY i.mcap_rank ASC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Filter by due date unless forced
    if not force:
        rows = [r for r in rows if is_due(r.get("last_takeaway_updated"), cadence)]

    if limit:
        rows = rows[:limit]

    log.info(f"refresh_takeaways [{tier}]: {len(rows)} companies to process")

    success, failed, skipped = 0, 0, 0
    batch_updates = []

    for i, row in enumerate(rows):
        if not row.get("overview"):
            skipped += 1
            continue

        prompt = _build_takeaway_prompt(row)
        takeaway = await _call_claude(prompt, max_tokens=700)

        if takeaway and len(takeaway) >= 200:
            batch_updates.append((takeaway, today, row["nse_code"]))
            success += 1
        else:
            log.warning(f"Short/failed takeaway for {row['nse_code']}: {takeaway[:50] if takeaway else 'None'}")
            failed += 1

        # Batch write every BATCH_SIZE
        if len(batch_updates) >= BATCH_SIZE:
            _write_takeaways(batch_updates)
            batch_updates = []

        await asyncio.sleep(RATE_SLEEP)

        if (i + 1) % 50 == 0:
            log.info(f"Progress: {i+1}/{len(rows)} — success={success} failed={failed}")

    # Write remaining
    if batch_updates:
        _write_takeaways(batch_updates)

    # Log run to session_log
    _log_run("takeaway", tier, success, failed, skipped, today)

    return {
        "status": "ok",
        "tier": tier,
        "run_date": str(today),
        "processed": len(rows),
        "success": success,
        "failed": failed,
        "skipped_no_overview": skipped,
        "next_due": str(next_quarterly_due() if tier == "top500" else next_annual_due()),
    }


async def refresh_overviews(
    force: bool = False,
    limit: Optional[int] = None,
) -> Dict:
    """Refresh overview for all 1700+ companies. Annual run."""
    today = date.today()

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
                i.nse_code, i.company_name, i.mcap_rank, i.last_overview_updated,
                s.market_cap, s."Promoter holding" AS promoter_holding,
                s.industry_group, s."Industry" AS industry
            FROM input_raw i
            JOIN screener_raw s ON i.nse_code = s.nse_code
            ORDER BY i.mcap_rank ASC NULLS LAST
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    if not force:
        rows = [r for r in rows if is_due(r.get("last_overview_updated"), "annual")]

    if limit:
        rows = rows[:limit]

    log.info(f"refresh_overviews: {len(rows)} companies to process")

    success, failed = 0, 0
    batch_updates = []

    for i, row in enumerate(rows):
        prompt = _build_overview_prompt(row)
        overview = await _call_claude(prompt, max_tokens=500)

        if overview and len(overview) >= 150:
            batch_updates.append((overview, today, row["nse_code"]))
            success += 1
        else:
            log.warning(f"Short/failed overview for {row['nse_code']}")
            failed += 1

        if len(batch_updates) >= BATCH_SIZE:
            _write_overviews(batch_updates)
            batch_updates = []

        await asyncio.sleep(RATE_SLEEP)

        if (i + 1) % 50 == 0:
            log.info(f"Progress: {i+1}/{len(rows)} — success={success} failed={failed}")

    if batch_updates:
        _write_overviews(batch_updates)

    _log_run("overview", "all_1700", success, failed, 0, today)

    return {
        "status": "ok",
        "run_date": str(today),
        "processed": len(rows),
        "success": success,
        "failed": failed,
        "next_due": str(next_annual_due()),
    }


# ── DB writers ────────────────────────────────────────────────────────────────

def _write_takeaways(batch: List[tuple]):
    """batch: [(takeaway, date, nse_code), ...]"""
    with _conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            UPDATE input_raw
            SET key_takeaway = %s, last_takeaway_updated = %s
            WHERE nse_code = %s
        """, batch)
        conn.commit()
    log.info(f"Wrote {len(batch)} takeaways to input_raw")


def _write_overviews(batch: List[tuple]):
    """batch: [(overview, date, nse_code), ...]"""
    with _conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            UPDATE input_raw
            SET overview = %s, last_overview_updated = %s
            WHERE nse_code = %s
        """, batch)
        conn.commit()
    log.info(f"Wrote {len(batch)} overviews to input_raw")


# ── Session log ───────────────────────────────────────────────────────────────

def _log_run(field: str, tier: str, success: int, failed: int, skipped: int, run_date: date):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_log (category, title, details, session_ts)
                VALUES (%s, %s, %s::jsonb, NOW())
            """, (
                "refresh_run",
                f"{field}_refresh_{tier}",
                json.dumps({
                    "field": field,
                    "tier": tier,
                    "run_date": str(run_date),
                    "success": success,
                    "failed": failed,
                    "skipped": skipped,
                }),
            ))
            conn.commit()
    except Exception as e:
        log.warning(f"_log_run failed: {e}")


# ── Due date checker (for scheduler + digest) ─────────────────────────────────

def get_refresh_status() -> Dict:
    """Returns current refresh status and next due dates for all 4 schedules."""
    with _conn() as conn, conn.cursor() as cur:
        # Takeaway top 500
        cur.execute("""
            SELECT MIN(last_takeaway_updated), MAX(last_takeaway_updated), COUNT(*)
            FROM input_raw WHERE mcap_rank <= 500
        """)
        r = cur.fetchone()
        t500_min, t500_max, t500_count = r

        # Takeaway 501-1700
        cur.execute("""
            SELECT MIN(last_takeaway_updated), MAX(last_takeaway_updated), COUNT(*)
            FROM input_raw WHERE mcap_rank BETWEEN 501 AND 1700
        """)
        r = cur.fetchone()
        t_mid_min, t_mid_max, t_mid_count = r

        # Overview all
        cur.execute("""
            SELECT MIN(last_overview_updated), MAX(last_overview_updated), COUNT(*)
            FROM input_raw
        """)
        r = cur.fetchone()
        ov_min, ov_max, ov_count = r

        # Last runs
        cur.execute("""
            SELECT title, details::text, session_ts
            FROM session_log
            WHERE category = 'refresh_run'
            ORDER BY session_ts DESC LIMIT 10
        """)
        last_runs = [{"title": r[0], "details": r[1], "ts": str(r[2])} for r in cur.fetchall()]

    today = date.today()

    def days_ago(d):
        return (today - d).days if d else None

    return {
        "today": str(today),
        "schedules": {
            "takeaway_top500": {
                "scope": "mcap_rank <= 500",
                "count": t500_count,
                "last_updated_min": str(t500_min) if t500_min else None,
                "last_updated_max": str(t500_max) if t500_max else None,
                "days_since_oldest": days_ago(t500_min),
                "cadence": "quarterly",
                "next_due": str(next_quarterly_due()),
                "due_now": is_due(t500_min, "quarterly"),
            },
            "takeaway_501_1700": {
                "scope": "mcap_rank 501-1700",
                "count": t_mid_count,
                "last_updated_min": str(t_mid_min) if t_mid_min else None,
                "cadence": "annual",
                "next_due": str(next_annual_due()),
                "due_now": is_due(t_mid_min, "annual"),
            },
            "overview_all": {
                "scope": "all 1700+",
                "count": ov_count,
                "last_updated_min": str(ov_min) if ov_min else None,
                "cadence": "annual",
                "next_due": str(next_annual_due()),
                "due_now": is_due(ov_min, "annual"),
            },
        },
        "last_runs": last_runs,
    }
