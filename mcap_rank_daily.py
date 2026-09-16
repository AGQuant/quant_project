"""
mcap_rank_daily.py -- cc#2125: market-cap rank, PER DATE, from screener_raw.

WHY: input_raw.mcap_rank / cap_category froze in June 2026 -- no job ever recomputed them and the
last input_raw load was 2026-07-18. session_log 85 (cap_category_definition, locked 04-Jun-2026)
always named the source as "mcap_rank from screener_raw.market_cap ranked DESC"; the implementation
drifted to input_raw and stopped there. This module restores the locked spec: the rank is computed
from screener_raw on every screener CSV load and written PER DATE, so a cap band is point-in-time
reproducible -- the property the Quant Basket universe builder (cc#2123) depends on.

ONE derivation, in RANK_SQL below: ROW_NUMBER() OVER (ORDER BY market_cap DESC, nse_code) -- the
same ordering the live basket selectors already use (qb_composite_select / qb_smallcap_select rank
with ROW_NUMBER over market_cap DESC), gapless, with the nse_code tie-break making ties
deterministic. Band edges are session_log 85's, verbatim, and the CASE in RANK_SQL is the only
place they live in this file: large 1-100 | mid 101-250 | small 251-1000 | micro 1001+.

Additive: a NEW table, no ALTER on input_raw (MAINTENANCE_LOCK_RULE). input_raw.mcap_rank and
cap_category are left in place and simply stop being read by each surface as it is cut over.
Hooked into gvm_nightly._sql_clean_replace_screener_v2 -- the one real writer to screener_raw,
which both loader paths go through -- so the rank can never again lag a CSV upload; no separate
schedule the founder has to remember (spec item 1). A same-day re-run replaces that ONE date's
rows (a corrected re-upload the same day wins); rows for any other date are never touched.
"""
import logging
import os
from datetime import date
from typing import Optional

import psycopg
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.mcap_rank_daily")
router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mcap_rank_daily (
    symbol            text        NOT NULL,
    rank_date         date        NOT NULL,
    market_cap        numeric     NOT NULL,
    mcap_rank         integer     NOT NULL,
    cap_category      text        NOT NULL,
    source_loaded_at  timestamp,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, rank_date)
)
"""
INDEX_SQL = ("CREATE INDEX IF NOT EXISTS mcap_rank_daily_date_rank_idx "
             "ON mcap_rank_daily (rank_date, mcap_rank)")

RANK_SQL = """
INSERT INTO mcap_rank_daily (symbol, rank_date, market_cap, mcap_rank, cap_category, source_loaded_at)
SELECT nse_code, %(d)s, market_cap, r,
       CASE WHEN r <= 100 THEN 'large' WHEN r <= 250 THEN 'mid'
            WHEN r <= 1000 THEN 'small' ELSE 'micro' END,
       loaded_at
  FROM (SELECT nse_code, market_cap, loaded_at,
               ROW_NUMBER() OVER (ORDER BY market_cap DESC, nse_code) AS r
          FROM screener_raw
         WHERE nse_code IS NOT NULL AND nse_code <> '' AND market_cap IS NOT NULL) s
"""


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        cur.execute(INDEX_SQL)
    conn.commit()


def recompute_mcap_rank(conn=None, rank_date: Optional[date] = None) -> dict:
    """Rank screener_raw's current batch and write it under rank_date (default: the batch's own
    loaded_at::date). Replaces that ONE date's rows, never any other date. Returns the row count
    and per-band counts so the caller can show them (cc#804's one-glance diagnostics shape)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(loaded_at) FROM screener_raw")
            loaded_at = cur.fetchone()[0]
            if loaded_at is None:
                return {"status": "no_data", "note": "screener_raw is empty; nothing to rank"}
            d = rank_date or loaded_at.date()
            cur.execute("DELETE FROM mcap_rank_daily WHERE rank_date = %(d)s", {"d": d})
            cur.execute(RANK_SQL, {"d": d})
            n = cur.rowcount
            cur.execute("SELECT cap_category, COUNT(*) FROM mcap_rank_daily "
                        "WHERE rank_date = %(d)s GROUP BY cap_category", {"d": d})
            bands = {k: v for k, v in cur.fetchall()}
        conn.commit()
        log.info("mcap_rank_daily: rank_date=%s rows=%d bands=%s source_loaded_at=%s",
                 d, n, bands, loaded_at)
        return {"status": "ok", "rank_date": str(d), "rows": n, "bands": bands,
                "source_loaded_at": str(loaded_at)}
    finally:
        if own:
            conn.close()


def latest_rank_date(cur) -> Optional[date]:
    cur.execute("SELECT MAX(rank_date) FROM mcap_rank_daily")
    return cur.fetchone()[0]


@router.get("/api/qb/mcap_rank/status")
def mcap_rank_status():
    """Latest rank date, row count, per-band counts, and whether a newer screener batch exists that
    the rank has not followed -- the exact failure class cc#2125 found, surfaced rather than silent."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.mcap_rank_daily')")
        if cur.fetchone()[0] is None:
            return {"status": "no_table", "note": "mcap_rank_daily has not been created yet"}
        d = latest_rank_date(cur)
        if d is None:
            return {"status": "empty", "note": "no rank has been written yet"}
        cur.execute("SELECT cap_category, COUNT(*) FROM mcap_rank_daily "
                    "WHERE rank_date = %(d)s GROUP BY cap_category", {"d": d})
        bands = {k: v for k, v in cur.fetchall()}
        cur.execute("SELECT COUNT(DISTINCT rank_date) FROM mcap_rank_daily")
        n_dates = cur.fetchone()[0]
        cur.execute("SELECT MAX(loaded_at) FROM screener_raw")
        loaded_at = cur.fetchone()[0]
    behind = bool(loaded_at and loaded_at.date() > d)
    return {"status": "ok", "rank_date": str(d), "rows": sum(bands.values()), "bands": bands,
            "dates_stored": n_dates,
            "screener_loaded_at": str(loaded_at) if loaded_at else None,
            "rank_behind_screener": behind,
            "note": ("a newer screener_raw batch exists that has not been ranked -- "
                     "POST /api/admin/recompute_mcap_rank" if behind else None)}


@router.post("/api/admin/recompute_mcap_rank")
def recompute_mcap_rank_endpoint(x_admin_token: Optional[str] = Header(None)):
    """Manual trigger (admin token). The normal path is the screener CSV load hook; this exists so
    a rank can be re-run without re-uploading the CSV, and so the first run has a callable path."""
    _check_admin(x_admin_token)
    try:
        return recompute_mcap_rank()
    except Exception as e:
        log.error("recompute_mcap_rank failed: %s", e, exc_info=True)
        raise HTTPException(500, f"recompute_mcap_rank failed: {e}")
