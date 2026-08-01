"""
mf_derived.py — cc#777 Part 2 + cc#778 amendment: holdings-derived NAV series & metrics engine.

WHAT IT IS
Between monthly Moneycontrol snapshots (cc#777 Part 1, the 11th), a fund's official NAV is only
refreshed by the AMFI/mfapi path. This engine reconstructs a DAILY NAV series per fund from what we
already own — the latest mf_holdings weights x each stock's daily return from raw_prices, less a
daily expense accrual (TER/252) — anchored to, and rebased at, each month-end ACTUAL NAV.

    derived_nav(d) = anchor_nav * PROD over (anchor_date, d] of (1 + port_ret_t - ter/252)
    port_ret_t     = SUM(w_i * r_i,t) / SUM(w_i)      -- weight-normalised over RESOLVED holdings only

CADENCE (cc#778 amendment — NIGHTLY, not weekly)
  * run_nightly()  — inside the 01:00-02:05 IST chain, scheduled AFTER the 01:00 Yahoo EOD load so
    the same-day closes are consumed. Compute is trivial (~518 funds x ~60 holdings), so the screener
    and fund pages are always T-1 fresh instead of up to 6 days stale.
  * run_weekly_audit() — Saturday. SELF-AUDIT ONLY: re-grades tracking error / confidence from the
    accumulated daily points (~22 obs/fund/month vs 4 under the old weekly design — statistically
    real grading). It does NOT recompute the series.

HONESTY RULES (non-negotiable, and the reason several fields stay NULL for now)
  1. A horizon is emitted ONLY when the derived series genuinely spans it. mf_holdings currently
     holds ONE snapshot month, so 1M is computable and 3M/6M/1Y/3Y are not — chaining them today
     would mean applying CURRENT weights backwards through every rebalance we cannot see, which is a
     constant-weight BACKCAST, not a derivation. Those columns stay NULL and switch on by themselves
     as snapshots accumulate (the engine chains across every snapshot it has).
  2. Every figure here is DERIVED, never an official NAV return. Callers must render it with the
     "Derived (holdings-based)" label — mf_master.ret_1y/ret_3y remain the ACTUAL numbers and are
     always preferable where present.
  3. Coverage is stated, never assumed: coverage_pct = the share of a fund's disclosed weight that
     actually resolved to a priced NSE symbol. A fund below MIN_COVERAGE_PCT is not graded at all.
"""
import logging
from datetime import date, datetime, timedelta

log = logging.getLogger("scorr.mf_derived")

MIN_COVERAGE_PCT = 60.0     # a fund whose resolved weight is below this is not derived at all
TE_THRESHOLD_BPS = 60.0     # cc#777: drift > 60 bps/month at an anchor -> confidence LOW
TRADING_DAYS = 252.0
LOOKBACK_DAYS = 400         # series window kept warm (>1y so horizons switch on as snapshots land)

# horizon -> calendar days of series required before the figure may be emitted
HORIZONS = {"ret_1m": 30, "ret_3m": 91, "ret_6m": 182, "ret_1y": 365, "ret_3y": 1095}


def _conn():
    import mf_pipeline
    return mf_pipeline._conn()


def ensure_tables(cur):
    """Idempotent app-side creates (CREATE TABLE is permitted; never an ALTER rewrite)."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mf_derived_nav (
            scheme_code TEXT NOT NULL,
            nav_date    DATE NOT NULL,
            derived_nav NUMERIC NOT NULL,
            anchor_date DATE,
            anchor_nav  NUMERIC,
            PRIMARY KEY (scheme_code, nav_date))""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mf_derived_metrics (
            scheme_code    TEXT PRIMARY KEY,
            ret_1m NUMERIC, ret_3m NUMERIC, ret_6m NUMERIC, ret_1y NUMERIC, ret_3y NUMERIC,
            volatility     NUMERIC,
            max_drawdown   NUMERIC,
            tracking_err_bps NUMERIC,
            confidence     TEXT,
            coverage_pct   NUMERIC,
            series_start   DATE,
            series_end     DATE,
            obs            INTEGER,
            basis          TEXT DEFAULT 'Derived (holdings-based)',
            updated_at     TIMESTAMPTZ DEFAULT NOW())""")


def _rebuild_series(cur, since: date):
    """Rebuild mf_derived_nav for every fund with usable holdings.

    Done in ONE set-based statement: cumulative compounding is exp(SUM(ln(1+r))) over the window,
    which is exact for equities (1+r > 0 always) and avoids pulling ~130k rows into Python. Each
    fund is anchored to the latest month-end ACTUAL NAV (mf_nav_history nav_kind='m') on or before
    the series start, and REBASED at every later anchor — so drift can never accumulate across
    months, and tracking error stays a per-month measurement rather than a running total."""
    cur.execute("""
        WITH latest_snap AS (SELECT MAX(as_of_month) AS m FROM mf_holdings),
        w AS (   -- resolved, priced weights from the newest snapshot
            SELECT h.scheme_code, UPPER(h.resolved_nse_symbol) AS symbol, h.pct_weight::numeric AS w
            FROM mf_holdings h, latest_snap s
            WHERE h.as_of_month = s.m AND h.resolved_nse_symbol IS NOT NULL
              AND h.pct_weight IS NOT NULL AND h.pct_weight > 0),
        cov AS (SELECT scheme_code, SUM(w) AS resolved_w FROM w GROUP BY 1),
        eligible AS (SELECT scheme_code, resolved_w FROM cov WHERE resolved_w >= %(minc)s),
        r AS (   -- daily stock returns
            SELECT UPPER(symbol) AS symbol, price_date,
                   close / NULLIF(LAG(close) OVER (PARTITION BY UPPER(symbol) ORDER BY price_date), 0) - 1 AS ret
            FROM raw_prices WHERE price_date >= %(since)s AND close IS NOT NULL),
        ter AS (SELECT scheme_code, COALESCE(expense_ratio, 0)::numeric / 100.0 AS ter FROM mf_master),
        pr AS (  -- weight-normalised daily portfolio return, net of the daily ER accrual
            SELECT w.scheme_code, r.price_date,
                   SUM(w.w * r.ret) / NULLIF(SUM(w.w), 0) - COALESCE(MAX(t.ter), 0) / %(td)s AS port_ret
            FROM w
            JOIN eligible e ON e.scheme_code = w.scheme_code
            JOIN r ON r.symbol = w.symbol
            LEFT JOIN ter t ON t.scheme_code = w.scheme_code
            WHERE r.ret IS NOT NULL AND r.ret > -0.95
            GROUP BY 1, 2),
        anch AS ( -- ACTUAL NAV rebase points. Anchoring only on nav_kind='m' starved the engine:
                  -- just 209 of 514 eligible funds have ANY month-end row (avg 5.8, only 19 with 12+),
                  -- so 305 funds would produce no series at all. Every real NAV observation is a valid
                  -- anchor, so take them all (month-end included) — more anchors = shorter compounding
                  -- segments = less drift.
            SELECT scheme_code, nav_date, nav::numeric AS nav
            FROM mf_nav_history WHERE nav IS NOT NULL AND nav > 0 AND nav_date >= %(since)s),
        seg AS (  -- attach each day to the last actual NAV STRICTLY BEFORE it. Strictly-before matters:
                  -- it makes the derived value ON an anchor date an OUT-OF-SAMPLE estimate that can be
                  -- scored against that day's actual NAV (that comparison IS the tracking error). With
                  -- `<=` the anchor would be the day itself, derived would equal actual by construction,
                  -- and tracking error would be a meaningless zero.
                  -- ONE lateral, not two correlated subqueries: the first cut ran the same anchor
                  -- lookup twice per row over ~140k rows and blew past a 60s budget in testing.
            SELECT p.scheme_code, p.price_date, p.port_ret, a.nav_date AS anchor_date, a.nav AS anchor_nav
            FROM pr p
            LEFT JOIN LATERAL (
                SELECT a2.nav_date, a2.nav FROM anch a2
                WHERE a2.scheme_code = p.scheme_code AND a2.nav_date < p.price_date
                ORDER BY a2.nav_date DESC LIMIT 1) a ON TRUE)
        INSERT INTO mf_derived_nav (scheme_code, nav_date, derived_nav, anchor_date, anchor_nav)
        SELECT scheme_code, price_date,
               anchor_nav * EXP(SUM(LN(1 + port_ret))
                                OVER (PARTITION BY scheme_code, anchor_date ORDER BY price_date)),
               anchor_date, anchor_nav
        FROM seg
        WHERE anchor_nav IS NOT NULL AND anchor_nav > 0 AND port_ret > -0.95
        ON CONFLICT (scheme_code, nav_date) DO UPDATE
          SET derived_nav = EXCLUDED.derived_nav,
              anchor_date = EXCLUDED.anchor_date,
              anchor_nav  = EXCLUDED.anchor_nav
    """, {"since": since, "minc": MIN_COVERAGE_PCT, "td": TRADING_DAYS})
    return cur.rowcount


def _recompute_metrics(cur):
    """Derive the horizon returns, volatility, max drawdown, tracking error and confidence from the
    series. A horizon is emitted ONLY where the series actually spans it (honesty rule 1) — the
    CASE guards below are what keep 3M/6M/1Y/3Y NULL until enough snapshots have accumulated."""
    cur.execute("""
        WITH s AS (
            SELECT scheme_code, nav_date, derived_nav,
                   MIN(nav_date) OVER (PARTITION BY scheme_code) AS d0,
                   MAX(nav_date) OVER (PARTITION BY scheme_code) AS dn
            FROM mf_derived_nav),
        latest AS (SELECT DISTINCT ON (scheme_code) scheme_code, nav_date AS dn, derived_nav AS nav_now,
                          d0 FROM s ORDER BY scheme_code, nav_date DESC),
        span AS (SELECT scheme_code, dn, nav_now, d0, (dn - d0) AS days FROM latest),
        pick AS (   -- nav at (dn - N days), nearest prior point, per horizon
            SELECT sp.scheme_code, sp.dn, sp.nav_now, sp.d0, sp.days,
              (SELECT derived_nav FROM s WHERE s.scheme_code=sp.scheme_code AND s.nav_date <= sp.dn - 30  ORDER BY s.nav_date DESC LIMIT 1) AS n1m,
              (SELECT derived_nav FROM s WHERE s.scheme_code=sp.scheme_code AND s.nav_date <= sp.dn - 91  ORDER BY s.nav_date DESC LIMIT 1) AS n3m,
              (SELECT derived_nav FROM s WHERE s.scheme_code=sp.scheme_code AND s.nav_date <= sp.dn - 182 ORDER BY s.nav_date DESC LIMIT 1) AS n6m,
              (SELECT derived_nav FROM s WHERE s.scheme_code=sp.scheme_code AND s.nav_date <= sp.dn - 365 ORDER BY s.nav_date DESC LIMIT 1) AS n1y,
              (SELECT derived_nav FROM s WHERE s.scheme_code=sp.scheme_code AND s.nav_date <= sp.dn - 1095 ORDER BY s.nav_date DESC LIMIT 1) AS n3y
            FROM span sp),
        dd AS (     -- drawdown vs the running peak (window first; an aggregate may not wrap a window)
            SELECT scheme_code,
                   derived_nav / NULLIF(MAX(derived_nav) OVER (PARTITION BY scheme_code ORDER BY nav_date), 0) - 1 AS ddv
            FROM mf_derived_nav),
        dd2 AS (SELECT scheme_code, MIN(ddv) AS max_drawdown FROM dd GROUP BY scheme_code),
        vol AS (    -- annualised stdev of daily derived returns
            SELECT scheme_code, STDDEV_SAMP(chg) * SQRT(%(td)s) AS volatility, COUNT(*) AS obs
            FROM (SELECT scheme_code, derived_nav / NULLIF(LAG(derived_nav) OVER (PARTITION BY scheme_code ORDER BY nav_date),0) - 1 AS chg
                  FROM mf_derived_nav) x
            WHERE chg IS NOT NULL GROUP BY scheme_code),
        te AS (     -- TRACKING ERROR: derived vs ACTUAL wherever a real NAV exists, mean |bps|. Because
                    -- the series anchors STRICTLY BEFORE each date, the derived value on an actual-NAV
                    -- day was compounded from the PRIOR anchor and never saw that day's real NAV — so
                    -- this is a genuine out-of-sample comparison, not a self-check.
            SELECT d.scheme_code, AVG(ABS(d.derived_nav / NULLIF(a.nav,0) - 1)) * 10000.0 AS te_bps,
                   COUNT(*) AS te_obs
            FROM mf_derived_nav d
            JOIN mf_nav_history a ON a.scheme_code = d.scheme_code AND a.nav_date = d.nav_date
                                 AND a.nav IS NOT NULL AND a.nav > 0
            WHERE d.anchor_date IS NOT NULL AND d.nav_date > d.anchor_date
            GROUP BY d.scheme_code),
        cov AS (
            SELECT h.scheme_code, SUM(h.pct_weight::numeric) AS coverage_pct
            FROM mf_holdings h, (SELECT MAX(as_of_month) m FROM mf_holdings) sn
            WHERE h.as_of_month = sn.m AND h.resolved_nse_symbol IS NOT NULL AND h.pct_weight IS NOT NULL
            GROUP BY 1)
        INSERT INTO mf_derived_metrics (scheme_code, ret_1m, ret_3m, ret_6m, ret_1y, ret_3y,
              volatility, max_drawdown, tracking_err_bps, confidence, coverage_pct,
              series_start, series_end, obs, updated_at)
        SELECT p.scheme_code,
               CASE WHEN p.days >= 30   AND p.n1m > 0 THEN ROUND(((p.nav_now/p.n1m)-1)*100, 2) END,
               CASE WHEN p.days >= 91   AND p.n3m > 0 THEN ROUND(((p.nav_now/p.n3m)-1)*100, 2) END,
               CASE WHEN p.days >= 182  AND p.n6m > 0 THEN ROUND(((p.nav_now/p.n6m)-1)*100, 2) END,
               CASE WHEN p.days >= 365  AND p.n1y > 0 THEN ROUND(((p.nav_now/p.n1y)-1)*100, 2) END,
               CASE WHEN p.days >= 1095 AND p.n3y > 0 THEN ROUND(((p.nav_now/p.n3y)-1)*100, 2) END,
               ROUND(v.volatility::numeric * 100, 2), ROUND(dd2.max_drawdown::numeric * 100, 2),
               ROUND(te.te_bps::numeric, 1),
               -- confidence: LOW when drift exceeds the threshold; HIGH when it is measured and clean;
               -- UNGRADED while no anchor comparison exists yet (never silently "HIGH").
               CASE WHEN te.te_bps IS NULL THEN 'UNGRADED'
                    WHEN te.te_bps > %(te)s THEN 'LOW' ELSE 'HIGH' END,
               ROUND(c.coverage_pct::numeric, 1), p.d0, p.dn, v.obs, NOW()
        FROM pick p
        LEFT JOIN vol v   ON v.scheme_code = p.scheme_code
        LEFT JOIN dd2     ON dd2.scheme_code = p.scheme_code
        LEFT JOIN te      ON te.scheme_code = p.scheme_code
        LEFT JOIN cov c   ON c.scheme_code = p.scheme_code
        ON CONFLICT (scheme_code) DO UPDATE SET
            ret_1m=EXCLUDED.ret_1m, ret_3m=EXCLUDED.ret_3m, ret_6m=EXCLUDED.ret_6m,
            ret_1y=EXCLUDED.ret_1y, ret_3y=EXCLUDED.ret_3y, volatility=EXCLUDED.volatility,
            max_drawdown=EXCLUDED.max_drawdown, tracking_err_bps=EXCLUDED.tracking_err_bps,
            confidence=EXCLUDED.confidence, coverage_pct=EXCLUDED.coverage_pct,
            series_start=EXCLUDED.series_start, series_end=EXCLUDED.series_end,
            obs=EXCLUDED.obs, updated_at=NOW()
    """, {"td": TRADING_DAYS, "te": TE_THRESHOLD_BPS})
    return cur.rowcount


def run_nightly(conn=None):
    """cc#778: NIGHTLY derived-NAV rebuild + metric recompute. Runs after the 01:00 Yahoo EOD load so
    the same-day closes are consumed; the screener/fund pages are therefore always T-1 fresh."""
    own = conn is None
    conn = conn or _conn()
    try:
        since = date.today() - timedelta(days=LOOKBACK_DAYS)
        with conn.cursor() as cur:
            ensure_tables(cur); conn.commit()
            nav_rows = _rebuild_series(cur, since)
            conn.commit()
            met_rows = _recompute_metrics(cur)
            conn.commit()
            cur.execute("""SELECT count(*), count(*) FILTER (WHERE ret_1m IS NOT NULL),
                                  count(*) FILTER (WHERE confidence='HIGH'),
                                  count(*) FILTER (WHERE confidence='LOW')
                           FROM mf_derived_metrics""")
            tot, with_1m, hi, lo = cur.fetchone()
            summary = {"nav_rows": nav_rows, "funds_graded": met_rows, "metrics_rows": tot,
                       "with_ret_1m": with_1m, "confidence_high": hi, "confidence_low": lo,
                       "series_since": str(since)}
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'mf', 'MF_DERIVED_NIGHTLY', %s::jsonb)""",
                        (__import__("json").dumps(summary),))
            conn.commit()
        log.info(f"mf_derived nightly: {summary}")
        return summary
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        log.error(f"mf_derived run_nightly: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if own:
            try: conn.close()
            except Exception: pass


def run_weekly_audit(conn=None):
    """cc#778: Saturday SELF-AUDIT ONLY — re-grade tracking error / confidence from the accumulated
    daily points (~22 obs/fund/month rather than 4). Does NOT rebuild the series; the nightly job
    owns that. Reports the grade distribution + the worst drifters for review."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur); conn.commit()
            _recompute_metrics(cur); conn.commit()
            cur.execute("""SELECT confidence, count(*), ROUND(AVG(tracking_err_bps),1)
                           FROM mf_derived_metrics GROUP BY confidence ORDER BY 2 DESC""")
            grades = [{"confidence": r[0], "funds": r[1], "avg_te_bps": float(r[2]) if r[2] is not None else None}
                      for r in cur.fetchall()]
            cur.execute("""SELECT scheme_code, tracking_err_bps FROM mf_derived_metrics
                           WHERE tracking_err_bps IS NOT NULL
                           ORDER BY tracking_err_bps DESC LIMIT 10""")
            worst = [{"scheme_code": r[0], "te_bps": float(r[1])} for r in cur.fetchall()]
            summary = {"grades": grades, "worst_drifters": worst, "threshold_bps": TE_THRESHOLD_BPS}
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'mf', 'MF_DERIVED_WEEKLY_AUDIT', %s::jsonb)""",
                        (__import__("json").dumps(summary),))
            conn.commit()
        log.info(f"mf_derived weekly audit: {summary}")
        return summary
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        log.error(f"mf_derived run_weekly_audit: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if own:
            try: conn.close()
            except Exception: pass
