"""v8_era.py — cc#1604 V8_ERA_CUTOVER_ONLY_V1 (session_log 36757, founder 02-Sep-2026).

ONE place that answers three questions every V8 performance surface asks:

    cutover_ts(cur)          -> the era start, app_config.v8_paper_rebuild_cutover_ts (ISO string)
    era_label(cur)           -> "Since 18-Jul-2026" — the cutover DATE in IST, read at render,
                                never typed anywhere
    full_ledger_allowed(cur) -> False while app_config.v8_full_ledger_suspended is true (default
                                true = suspended). The founder flips the flag; no code change.

Doctrine: V8 performance is reported for the post-cutover era ONLY. The full ledger since
inception is SUSPENDED — not computed, not served, not displayed (web, app, digest, reports, MCP).
An era=all request is refused with HTTP 410 and this module's `suspended_payload()`, never an
aggregate. Pre-cutover rows stay in v8_paper_trades untouched; they are simply outside every
computation window. Amends V8_PNL_CANON_V1 (18337) era handling; the formula itself is untouched.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
from fastapi import APIRouter

IST = ZoneInfo("Asia/Kolkata")
CUTOVER_KEY = "v8_paper_rebuild_cutover_ts"
SUSPEND_KEY = "v8_full_ledger_suspended"
router = APIRouter()


def _cfg(cur, key):
    cur.execute("SELECT value FROM app_config WHERE key=%s", (key,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def cutover_ts(cur):
    """The era start as stored (ISO string, naive IST), or None if app_config has no row."""
    return _cfg(cur, CUTOVER_KEY)


def cutover_date(cur):
    """The cutover DATE (IST) as a date, or None."""
    ts = cutover_ts(cur)
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace(" ", "T"))
        if d.tzinfo is not None:
            d = d.astimezone(IST)
        return d.date()
    except ValueError:
        return None


def era_label(cur):
    """'Since 18-Jul-2026' from the cutover date; 'Since cutover' only if no cutover is stored."""
    d = cutover_date(cur)
    return f"Since {d.strftime('%d-%b-%Y')}" if d else "Since cutover"


def full_ledger_allowed(cur):
    """False while v8_full_ledger_suspended is true. A MISSING flag also means suspended: the
    doctrine's default is suspended, and an unset flag must never quietly re-open the ledger."""
    v = _cfg(cur, SUSPEND_KEY)
    if v is None:
        return False
    return str(v).strip().lower() in ("false", "0", "no", "off")


def suspended_payload(cur):
    """The body every era-aware endpoint returns (with HTTP 410) for a full-ledger request."""
    d = cutover_date(cur)
    return {"error": "full ledger suspended", "era": "cutover",
            "since": d.isoformat() if d else None, "era_label": era_label(cur),
            "rule": "V8_ERA_CUTOVER_ONLY_V1 (session_log 36757)"}


def era_block(cur):
    """The caption block a surface prints under its KPI row — served, never typed."""
    d = cutover_date(cur)
    return {"era": "cutover", "since": d.isoformat() if d else None, "era_label": era_label(cur),
            "cutover_ts": cutover_ts(cur), "full_ledger_allowed": full_ledger_allowed(cur)}


@router.get("/api/v8/era")
def api_v8_era():
    """The era caption for any surface: {era, since, era_label, cutover_ts, full_ledger_allowed}."""
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        return era_block(cur)
