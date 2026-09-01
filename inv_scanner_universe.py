"""cc#1283 · INVESTMENT SCANNER ENGINE 1/3 — UNIVERSE BUILDER (spec session_log 30129 V1.1).

Builds the tagged 4-leg union the scanner's scoring (cc#1284) and rules (cc#1285) will read:

    LEG 1  quant basket open positions (status='open', notes NOT ILIKE 'Cash residual%'),
           one tag PER MEMBERSHIP: 'basket:<basket_name>' — a symbol held by three baskets
           carries three tags.
    LEG 2  momentum source: gvm_score > 7.8 AND 1-month dM >= +0.25  → 'screen:momentum'
    LEG 3  reversal source (founder-added 24-Aug): gvm_score > 7 AND 90-day dG > 0 AND
           90-day dV > 0 → 'screen:gv_rising' — quality-improving names regardless of price
           position, stocking the reversal pond leg 2 structurally excludes.
    LEG 4  Screeners-page presets (cc#1538, founder 31-Aug: "Quant screeners all including
           FINZ"): every member of every global v13 preset — v13_screen_results r JOIN
           v13_presets p ON p.id = r.screen_id WHERE COALESCE(p.scope,'global') = 'global',
           the exact query shape the Screeners page itself reads, so the leg and the page can
           never disagree about membership. One tag PER PRESET: 'screener:<preset name>'
           (e.g. 'screener:Momentum Kings', 'screener:Stable'). Scorr's algorithmic screens
           and the FINZ imports are deliberately ONE leg with one tag prefix — the preset
           name alone distinguishes them if that is ever needed.

DELTA CONVENTION (30129, same as every delta on the platform): closest-on-or-before —
today's pillar minus the pillar on the latest score_date <= (latest - N days). A name with
no row that far back gets NULL deltas and insufficient_history=true, NEVER a zero-filled
delta: a zero would read as "unchanged", which is a fabricated statement about a period the
data does not cover.

dgv_flags carries the 90-day pillar deltas as jsonb {"dg_90d": x, "dv_90d": y} — dm_1mo has
its own column because legs 2's gate reads it directly. (Field named by the card; this
interpretation is logged on the card for Fable's verify.)

The whole build is ONE SQL statement (BUILD_SQL below), so the nightly job, the admin
one-shot and any hand-run first-run evidence execute IDENTICAL logic — there is no second
implementation to drift. Reads gvm_scores + quant_paper_positions read-only; writes only
investment_scanner_universe. v8_* context untouched (isolation 244/324).
"""

import os
import logging
from typing import Optional

import psycopg
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.inv_scanner")
router = APIRouter(tags=["investment_scanner"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


DDL = """
CREATE TABLE IF NOT EXISTS investment_scanner_universe (
    symbol   TEXT NOT NULL,
    run_date DATE NOT NULL,
    tags     TEXT[] NOT NULL,
    gvm      NUMERIC,
    g        NUMERIC,
    v        NUMERIC,
    m        NUMERIC,
    dm_1mo   NUMERIC,
    dgv_flags JSONB,
    insufficient_history BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, run_date)
);
"""

# One statement, one truth. Re-runnable: ON CONFLICT refreshes the same (symbol, run_date).
BUILD_SQL = """
WITH latest AS (SELECT MAX(score_date) AS d FROM gvm_scores),
now_rows AS (
    SELECT DISTINCT ON (UPPER(symbol)) UPPER(symbol) AS symbol,
           gvm_score, g_score, v_score, m_score
    FROM gvm_scores WHERE score_date = (SELECT d FROM latest)
    ORDER BY UPPER(symbol), id DESC
),
m30 AS (
    -- gvm_scores holds ONLY the latest day; the series lives in gvm_history (2021->now,
    -- same pillar columns). Lookbacks therefore read gvm_history — measured before shipping:
    -- reading gvm_scores here returns zero rows 30 days back and legs 2/3 collapse to 0.
    SELECT DISTINCT ON (UPPER(h.symbol)) UPPER(h.symbol) AS symbol, h.m_score
    FROM gvm_history h, latest
    WHERE h.score_date <= latest.d - INTERVAL '30 days'
    ORDER BY UPPER(h.symbol), h.score_date DESC
),
m90 AS (
    SELECT DISTINCT ON (UPPER(h.symbol)) UPPER(h.symbol) AS symbol, h.g_score, h.v_score
    FROM gvm_history h, latest
    WHERE h.score_date <= latest.d - INTERVAL '90 days'
    ORDER BY UPPER(h.symbol), h.score_date DESC
),
base AS (
    SELECT n.symbol, n.gvm_score, n.g_score, n.v_score, n.m_score,
           CASE WHEN m30.symbol IS NULL THEN NULL ELSE n.m_score - m30.m_score END AS dm_1mo,
           CASE WHEN m90.symbol IS NULL THEN NULL ELSE n.g_score - m90.g_score END AS dg_90d,
           CASE WHEN m90.symbol IS NULL THEN NULL ELSE n.v_score - m90.v_score END AS dv_90d,
           (m30.symbol IS NULL OR m90.symbol IS NULL) AS insufficient_history
    FROM now_rows n
    LEFT JOIN m30 ON m30.symbol = n.symbol
    LEFT JOIN m90 ON m90.symbol = n.symbol
),
leg1 AS (
    SELECT UPPER(q.symbol) AS symbol, 'basket:' || q.basket_name AS tag
    FROM quant_paper_positions q
    WHERE q.status = 'open' AND q.notes NOT ILIKE 'Cash residual%'
),
leg2 AS (
    SELECT symbol, 'screen:momentum' AS tag FROM base
    WHERE gvm_score > 7.8 AND dm_1mo >= 0.25
),
leg3 AS (
    SELECT symbol, 'screen:gv_rising' AS tag FROM base
    WHERE gvm_score > 7 AND dg_90d > 0 AND dv_90d > 0
),
leg4 AS (
    -- cc#1538: every Screeners-page preset, scorr + finz together (see LEG 4 in the header)
    SELECT UPPER(r.symbol) AS symbol, 'screener:' || p.name AS tag
    FROM v13_screen_results r
    JOIN v13_presets p ON p.id = r.screen_id
    WHERE COALESCE(p.scope, 'global') = 'global'
),
uni AS (
    SELECT symbol, ARRAY_AGG(DISTINCT tag ORDER BY tag) AS tags
    FROM (SELECT * FROM leg1 UNION ALL SELECT * FROM leg2 UNION ALL SELECT * FROM leg3
          UNION ALL SELECT * FROM leg4) x
    GROUP BY symbol
)
INSERT INTO investment_scanner_universe
    (symbol, run_date, tags, gvm, g, v, m, dm_1mo, dgv_flags, insufficient_history)
SELECT u.symbol, (SELECT d FROM latest), u.tags,
       b.gvm_score, b.g_score, b.v_score, b.m_score,
       b.dm_1mo,
       CASE WHEN b.symbol IS NULL THEN NULL
            ELSE jsonb_build_object('dg_90d', b.dg_90d, 'dv_90d', b.dv_90d) END,
       COALESCE(b.insufficient_history, TRUE)
FROM uni u
LEFT JOIN base b ON b.symbol = u.symbol
ON CONFLICT (symbol, run_date) DO UPDATE SET
    tags = EXCLUDED.tags, gvm = EXCLUDED.gvm, g = EXCLUDED.g, v = EXCLUDED.v,
    m = EXCLUDED.m, dm_1mo = EXCLUDED.dm_1mo, dgv_flags = EXCLUDED.dgv_flags,
    insufficient_history = EXCLUDED.insufficient_history
"""


def build(conn=None) -> dict:
    """Run the union build for the latest GVM score_date. Returns honest counts —
    total rows, per-leg tag counts, multi-tag counts, insufficient-history count."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("SELECT MAX(score_date) FROM gvm_scores")
            d = cur.fetchone()[0]
            if d is None:
                return {"status": "skip", "reason": "gvm_scores empty — nothing to build from"}
            cur.execute(BUILD_SQL)
            n = cur.rowcount
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE tags && ARRAY['screen:momentum']),
                       COUNT(*) FILTER (WHERE tags && ARRAY['screen:gv_rising']),
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM UNNEST(tags) t WHERE t LIKE 'basket:%%')),
                       COUNT(*) FILTER (WHERE (SELECT COUNT(*) FROM UNNEST(tags) t WHERE t LIKE 'basket:%%') > 1),
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM UNNEST(tags) t WHERE t LIKE 'screener:%%')),
                       COUNT(*) FILTER (WHERE insufficient_history)
                FROM investment_scanner_universe WHERE run_date = %s""", (d,))
            tot, l2, l3, l1, multi_basket, l4, insuff = cur.fetchone()
        conn.commit()
        out = {"status": "ok", "run_date": str(d), "rows_upserted": n, "total": tot,
               "leg1_basket": l1, "leg2_momentum": l2, "leg3_gv_rising": l3,
               "leg4_screener": l4,
               "multi_basket_names": multi_basket, "insufficient_history": insuff}
        log.info(f"inv_scanner_universe: {out}")
        return out
    finally:
        if own:
            conn.close()


@router.post("/api/admin/run-inv-scanner-universe")
def admin_run(x_admin_token: Optional[str] = Header(None)):
    """One-shot manual build — cc#1172 admin pattern. The nightly job runs the same build()."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return build()


@router.get("/api/inv-scanner/universe")
def get_universe(run_date: Optional[str] = None, tag: Optional[str] = None):
    """Read the universe for a run_date (default latest). Optional tag filter (exact match
    against any tag). Read-only feed for cc#1284's scoring and cc#1286's tab."""
    with _conn() as conn, conn.cursor() as cur:
        if not run_date:
            cur.execute("SELECT MAX(run_date) FROM investment_scanner_universe")
            r = cur.fetchone()
            run_date = str(r[0]) if r and r[0] else None
        if not run_date:
            return {"run_date": None, "rows": []}
        q = """SELECT symbol, tags, gvm, g, v, m, dm_1mo, dgv_flags, insufficient_history
               FROM investment_scanner_universe WHERE run_date = %s"""
        args = [run_date]
        if tag:
            q += " AND %s = ANY(tags)"
            args.append(tag)
        q += " ORDER BY symbol"
        cur.execute(q, args)
        rows = [{"symbol": r[0], "tags": r[1],
                 "gvm": float(r[2]) if r[2] is not None else None,
                 "g": float(r[3]) if r[3] is not None else None,
                 "v": float(r[4]) if r[4] is not None else None,
                 "m": float(r[5]) if r[5] is not None else None,
                 "dm_1mo": float(r[6]) if r[6] is not None else None,
                 "dgv_flags": r[7], "insufficient_history": r[8]} for r in cur.fetchall()]
    return {"run_date": run_date, "count": len(rows), "rows": rows}
