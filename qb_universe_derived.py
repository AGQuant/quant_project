"""qb_universe_derived.py -- cc#2148: the QB Universe builder's NIGHT-WINDOW precompute layer.

WHAT THIS IS. Twelve of the locked forty universe filters need real computation -- quarterly
YoY arithmetic over fundamentals_history's metrics jsonb, and price-series maths over raw_prices --
so ruling_2_SERVER_LOAD clause (d) of cc#2123 keeps them OFF the request path: they run once a
night (02:30 IST, after GVM 01:30 and universe_technicals 02:05), write ONE EOD-stamped row per
symbol to qb_universe_derived, and the preview page reads that table with a plain join. Never on
request, never in market hours.

WHAT EACH COLUMN IS, stated once here and nowhere else:

  CAT_5  Quarterly Results, YoY -- fundamentals_history, section='quarters', period_type='quarter',
         CONSOLIDATED PREFERRED per (symbol, period_end) (cc#1865's rule, the same one
         results_endpoints reads by; the jsonb stores "OPM %" as text like "14%", so the sign is
         stripped before the cast). The arithmetic is results_endpoints._peer_comparison's, not
         a second one: latest quarter = MAX(period_end); year-ago = the row at period_end minus
         exactly one year; Sales|Revenue and "Net Profit" from the jsonb; a growth figure exists
         only when the base is > 0.
    yoy_sales_growth                 (Sales_latest / Sales_year_ago - 1) * 100
    yoy_profit_growth                (NetProfit_latest / NetProfit_year_ago - 1) * 100
    opm_latest_quarter               "OPM %" of the latest quarter (banks report a Financing
                                     Margin instead and carry NULL here -- not a zero)
    opm_change_yoy                   OPM latest - OPM same quarter last year, in percentage points
    consecutive_quarters_profit_growth  counting back from the latest quarter, how many quarters
                                     in a row beat their own year-ago Net Profit (both present,
                                     base > 0); stops at the first miss or gap
    quarters_since_last_result       whole quarters elapsed between the latest period_end and
                                     score_date (days / 91.31, floored); 0 = the latest quarter is
                                     the current reporting season
    latest_quarter_end               the period_end the row is built on

  CAT_6  Alpha & Risk -- raw_prices EOD closes (as-of lookups, never intraday), beta_daily.
         Every alpha is a DIFFERENCE in percentage points, never a ratio (Basket_Protocol
         Annexure C change 4).
    alpha_vs_nifty500_1y / _3y       stock return over the window minus NIFTY500's over the same
                                     window (raw_prices symbol NIFTY500)
    alpha_vs_sector_1y               stock 1y return minus the segment composite's 1y return, the
                                     composite being v12_backtest._segment_composite_series --
                                     fixed-weight (today's mcap share) price-return-chained, REUSED
                                     not re-derived, with the same trading-day calendar
    beta_1y / beta_sessions / beta_asof  the latest beta_daily row (benchmark NIFTY50). The table
                                     is a few days old, so this is FORWARD-ACCRUING: the window
                                     fills day by day and the page badges it so.
    max_drawdown_1y                  the deepest peak-to-trough fall over the last 365 days, in
                                     percent (negative): MIN(close / running-max close - 1) * 100
    rs_percentile_1y                 PERCENT_RANK of the 1y return across the scored universe,
                                     0..100 (100 = the strongest)

  CAT_3 remainder -- raw_prices EOD closes.
    ret_3m / ret_6m                  close / close as-of (price_date - 91 / 182 days) - 1, * 100;
                                     these light up the 3M / 6M options cc#2147 rendered disabled
    consecutive_up_months            counting back from the last COMPLETED calendar month, how
                                     many months in a row closed above the previous month's close;
                                     the running month is never counted (a month is a month at
                                     its close)

DATES. score_date = gvm_scores' latest score_date (the same universe and stamp the nightly chain
just built), so a weekend or holiday never gets a row of its own -- the boundary rolls forward by
construction (ENGINE_LIVENESS_RULE corollary). price_date = the last raw_prices session on or
before score_date, stated on the row. A score_date that already has rows is skipped unless the
caller says force -- the idempotent shape the other nightlies use.

MEMORY. Nothing here loads the price table into Python. Returns, drawdown and the percentile are
window functions in SQL over the 1y / 3y slice; the month-end closes are one small query; only the
segment composites walk through Python, one segment at a time. (cc#2153 found the night RSS
climb; this job must not add to it.)

SCHEDULING. scheduler._bg_qb_universe_derived at 02:30 IST daily. The tick-loop line is what
scheduler_master.enumerate_scheduler_jobs() parses, so the registry row exists by construction
and the drift audit will not retire it as "vanished from code". Manual trigger for the first-run
evidence: POST /api/admin/run_qb_universe_derived (admin token), which refuses to run during
market hours because the ruling does.
"""
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import psycopg
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.qb_universe_derived")
router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
TABLE = "qb_universe_derived"
BENCHMARK = "NIFTY500"

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    symbol                              text    NOT NULL,
    score_date                          date    NOT NULL,
    price_date                          date,
    -- CAT_5 quarterly results, YoY (fundamentals_history)
    yoy_sales_growth                    numeric,
    yoy_profit_growth                   numeric,
    opm_latest_quarter                  numeric,
    opm_change_yoy                      numeric,
    consecutive_quarters_profit_growth  integer,
    quarters_since_last_result          integer,
    latest_quarter_end                  date,
    -- CAT_6 alpha & risk (raw_prices, beta_daily)
    alpha_vs_nifty500_1y                numeric,
    alpha_vs_nifty500_3y                numeric,
    alpha_vs_sector_1y                  numeric,
    beta_1y                             numeric,
    beta_sessions                       integer,
    beta_asof                           date,
    max_drawdown_1y                     numeric,
    rs_percentile_1y                    numeric,
    -- CAT_3 remainder (raw_prices)
    ret_3m                              numeric,
    ret_6m                              numeric,
    consecutive_up_months               integer,
    computed_at                         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, score_date)
);
CREATE INDEX IF NOT EXISTS idx_qb_universe_derived_date ON {TABLE} (score_date DESC);
"""

_COLS = ("symbol", "score_date", "price_date",
         "yoy_sales_growth", "yoy_profit_growth", "opm_latest_quarter", "opm_change_yoy",
         "consecutive_quarters_profit_growth", "quarters_since_last_result", "latest_quarter_end",
         "alpha_vs_nifty500_1y", "alpha_vs_nifty500_3y", "alpha_vs_sector_1y",
         "beta_1y", "beta_sessions", "beta_asof", "max_drawdown_1y", "rs_percentile_1y",
         "ret_3m", "ret_6m", "consecutive_up_months")

UPSERT_SQL = (
    f"INSERT INTO {TABLE} (" + ", ".join(_COLS) + ", computed_at) VALUES ("
    + ", ".join("%%(%s)s" % c for c in _COLS) + ", NOW()) "
    "ON CONFLICT (symbol, score_date) DO UPDATE SET "
    + ", ".join("%s = EXCLUDED.%s" % (c, c) for c in _COLS if c not in ("symbol", "score_date"))
    + ", computed_at = NOW()"
)

# ── CAT_5: the quarterly rows, consolidated preferred, results_endpoints' arithmetic ─────────────
_QUARTERS_SQL = """
    WITH q AS (
        SELECT UPPER(f.symbol) AS sym, f.period_end, f.consolidated,
               NULLIF(replace(COALESCE(f.metrics->>'Sales', f.metrics->>'Revenue'), ',', ''), '')::numeric AS rev,
               NULLIF(replace(f.metrics->>'Net Profit', ',', ''), '')::numeric AS pat,
               NULLIF(replace(replace(f.metrics->>'OPM %%', ',', ''), '%%', ''), '')::numeric AS opm,
               ROW_NUMBER() OVER (PARTITION BY UPPER(f.symbol), f.period_end
                                  ORDER BY f.consolidated DESC, f.scraped_at DESC) AS rn
          FROM fundamentals_history f
         WHERE f.section = 'quarters' AND f.period_type = 'quarter'
           AND UPPER(f.symbol) = ANY(%(syms)s)
    )
    SELECT sym, period_end, rev, pat, opm FROM q WHERE rn = 1
     ORDER BY sym, period_end DESC
"""

# ── CAT_6 + CAT_3: as-of closes and window maths in SQL ─────────────────────────────────────────
_ASOF_SQL = """
    SELECT g.symbol,
           (SELECT close FROM raw_prices r WHERE r.symbol = g.symbol AND r.close IS NOT NULL
             AND r.price_date <= %(d)s ORDER BY r.price_date DESC LIMIT 1) AS c_now,
           (SELECT close FROM raw_prices r WHERE r.symbol = g.symbol AND r.close IS NOT NULL
             AND r.price_date <= %(d)s - 91 ORDER BY r.price_date DESC LIMIT 1) AS c_3m,
           (SELECT close FROM raw_prices r WHERE r.symbol = g.symbol AND r.close IS NOT NULL
             AND r.price_date <= %(d)s - 182 ORDER BY r.price_date DESC LIMIT 1) AS c_6m,
           (SELECT close FROM raw_prices r WHERE r.symbol = g.symbol AND r.close IS NOT NULL
             AND r.price_date <= %(d1y)s ORDER BY r.price_date DESC LIMIT 1) AS c_1y,
           (SELECT close FROM raw_prices r WHERE r.symbol = g.symbol AND r.close IS NOT NULL
             AND r.price_date <= %(d)s - 1095 ORDER BY r.price_date DESC LIMIT 1) AS c_3y
      FROM gvm_scores g
     WHERE g.score_date = %(score_date)s
"""
_BENCH_ASOF_SQL = """
    SELECT (SELECT close FROM raw_prices WHERE symbol = %(b)s AND close IS NOT NULL AND price_date <= %(d)s ORDER BY price_date DESC LIMIT 1),
           (SELECT close FROM raw_prices WHERE symbol = %(b)s AND close IS NOT NULL AND price_date <= %(d1y)s ORDER BY price_date DESC LIMIT 1),
           (SELECT close FROM raw_prices WHERE symbol = %(b)s AND close IS NOT NULL AND price_date <= %(d)s - 1095 ORDER BY price_date DESC LIMIT 1)
"""
_DRAWDOWN_SQL = """
    WITH w AS (
        SELECT r.symbol, r.price_date, r.close,
               MAX(r.close) OVER (PARTITION BY r.symbol ORDER BY r.price_date
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS peak
          FROM raw_prices r
          JOIN gvm_scores g ON g.symbol = r.symbol AND g.score_date = %(score_date)s
         WHERE r.price_date > %(d)s - 365 AND r.price_date <= %(d)s AND r.close IS NOT NULL AND r.close > 0
    )
    SELECT symbol, ROUND((MIN(close / peak) - 1) * 100, 2) FROM w GROUP BY symbol
"""
_MONTH_ENDS_SQL = """
    SELECT DISTINCT ON (r.symbol, date_trunc('month', r.price_date))
           r.symbol, date_trunc('month', r.price_date)::date AS m, r.close
      FROM raw_prices r
      JOIN gvm_scores g ON g.symbol = r.symbol AND g.score_date = %(score_date)s
     WHERE r.price_date > %(d)s - 800 AND r.price_date <= %(d)s AND r.close IS NOT NULL
     ORDER BY r.symbol, date_trunc('month', r.price_date), r.price_date DESC
"""
_BETA_SQL = """
    SELECT DISTINCT ON (symbol) symbol, beta, n_sessions, d
      FROM beta_daily
     WHERE symbol = ANY(%(syms)s) AND d <= %(d)s
     ORDER BY symbol, d DESC
"""
_CAL_SQL = """
    SELECT price_date FROM raw_prices
     WHERE symbol = %(b)s AND price_date >= %(start)s AND price_date <= %(d)s
     ORDER BY price_date
"""


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL", ""))


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _pct(a, b):
    """(a / b - 1) * 100 when both present and b > 0 -- the results_endpoints guard."""
    a, b = _f(a), _f(b)
    if a is None or b is None or b <= 0:
        return None
    return (a / b - 1.0) * 100.0


# ── pure helpers (unit-tested) ──────────────────────────────────────────────────────────────────
def profit_growth_streak(quarters: List[dict]) -> int:
    """quarters: newest first, each {period_end, pat}. Count how many quarters in a row, from the
    newest, beat their own year-ago Net Profit (year-ago = period_end minus one year exactly; both
    present, base > 0). Stops at the first miss or missing pair."""
    by_end = {q["period_end"]: q for q in quarters}
    n = 0
    for q in quarters:
        pe = q["period_end"]
        try:
            ya = pe.replace(year=pe.year - 1)
        except ValueError:            # 29-Feb
            ya = pe - timedelta(days=365)
        base = by_end.get(ya)
        g = _pct(q.get("pat"), base.get("pat")) if base else None
        if g is None or g <= 0:
            break
        n += 1
    return n


def quarters_since(latest_end: Optional[date], score_date: date) -> Optional[int]:
    if latest_end is None:
        return None
    days = (score_date - latest_end).days
    return max(0, int(math.floor(days / 91.31)))


def consecutive_up_months(month_closes: List[tuple], as_of: date) -> int:
    """month_closes: [(month_start_date, last_close_of_month)] oldest first. Counting back from the
    last COMPLETED month (the running month of `as_of` is excluded), how many months in a row
    closed above the previous month's close."""
    cur_m = date(as_of.year, as_of.month, 1)
    done = [(m, c) for m, c in month_closes if m < cur_m and c is not None]
    n = 0
    for i in range(len(done) - 1, 0, -1):
        m, c = done[i]
        pm, pc = done[i - 1]
        # months must be adjacent, or the streak is broken by a gap
        exp_prev = (m.replace(day=1) - timedelta(days=1)).replace(day=1)
        if pm != exp_prev or pc is None or pc <= 0 or c <= pc:
            break
        n += 1
    return n


def percent_ranks(values: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """0..100 PERCENT_RANK over the non-null values (ties share the lower rank)."""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    out = {k: None for k in values}
    if n <= 1:
        for _, k in items:
            out[k] = 100.0
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        pr = round(i / (n - 1) * 100.0, 2)
        for t in range(i, j + 1):
            out[items[t][1]] = pr
        i = j + 1
    return out


# ── the run ────────────────────────────────────────────────────────────────────────────────────
def run(conn=None, force: bool = False, limit: Optional[int] = None) -> dict:
    own = conn is None
    conn = conn or _conn()
    t0 = time.time()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            conn.commit()
            cur.execute("SELECT MAX(score_date) FROM gvm_scores")
            score_date = cur.fetchone()[0]
            if score_date is None:
                return {"status": "empty", "note": "gvm_scores has no rows", "rows": 0}
            cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE score_date = %s", (score_date,))
            existing = cur.fetchone()[0] or 0
            if existing and not force:
                return {"status": "skipped", "note": "already computed for this score_date",
                        "score_date": str(score_date), "rows": existing}
            cur.execute("SELECT MAX(price_date) FROM raw_prices WHERE price_date <= %s", (score_date,))
            d = cur.fetchone()[0] or score_date
            cur.execute("SELECT symbol, segment FROM gvm_scores WHERE score_date = %s ORDER BY symbol", (score_date,))
            uni = [(r[0], r[1]) for r in cur.fetchall()]
            if limit:
                uni = uni[:limit]
            syms = [s for s, _ in uni]
            seg_of = dict(uni)

            # 1y window on the benchmark's own trading-day calendar (the composite walks the same one)
            cur.execute(_CAL_SQL, {"b": BENCHMARK, "start": d - timedelta(days=365), "d": d})
            cal = [r[0] for r in cur.fetchall()]
            d1y = cal[0] if cal else d - timedelta(days=365)

            # ── CAT_5 ──
            cur.execute(_QUARTERS_SQL, {"syms": syms})
            qrows: Dict[str, List[dict]] = {}
            for sym, pe, rev, pat, opm in cur.fetchall():
                qrows.setdefault(sym, []).append({"period_end": pe, "rev": rev, "pat": pat, "opm": opm})
            cat5: Dict[str, dict] = {}
            for sym, qs in qrows.items():
                latest = qs[0]
                by_end = {q["period_end"]: q for q in qs}
                pe = latest["period_end"]
                try:
                    ya = pe.replace(year=pe.year - 1)
                except ValueError:
                    ya = pe - timedelta(days=365)
                base = by_end.get(ya)
                cat5[sym] = {
                    "yoy_sales_growth": _r(_pct(latest["rev"], base["rev"]) if base else None),
                    "yoy_profit_growth": _r(_pct(latest["pat"], base["pat"]) if base else None),
                    "opm_latest_quarter": _r(_f(latest["opm"])),
                    "opm_change_yoy": _r((_f(latest["opm"]) - _f(base["opm"]))
                                         if base and latest["opm"] is not None and base["opm"] is not None else None),
                    "consecutive_quarters_profit_growth": profit_growth_streak(qs),
                    "quarters_since_last_result": quarters_since(pe, score_date),
                    "latest_quarter_end": pe,
                }

            # ── CAT_6 + CAT_3: closes as-of ──
            cur.execute(_ASOF_SQL, {"d": d, "d1y": d1y, "score_date": score_date})
            asof = {r[0]: r[1:] for r in cur.fetchall()}
            cur.execute(_BENCH_ASOF_SQL, {"b": BENCHMARK, "d": d, "d1y": d1y})
            b_now, b_1y, b_3y = cur.fetchone()
            bench_1y, bench_3y = _pct(b_now, b_1y), _pct(b_now, b_3y)
            cur.execute(_DRAWDOWN_SQL, {"d": d, "score_date": score_date})
            mdd = {r[0]: _f(r[1]) for r in cur.fetchall()}
            cur.execute(_MONTH_ENDS_SQL, {"d": d, "score_date": score_date})
            months: Dict[str, List[tuple]] = {}
            for sym, m, c in cur.fetchall():
                months.setdefault(sym, []).append((m, _f(c)))
            cur.execute(_BETA_SQL, {"syms": syms, "d": d})
            beta = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

            # sector composites: one per segment, the reused v12 construction on the same calendar
            comp_ret: Dict[str, Optional[float]] = {}
            comp_note = {}
            if cal and len(cal) > 1:
                try:
                    from v12_backtest import _segment_composite_series
                    for seg in sorted({s for s in seg_of.values() if s}):
                        try:
                            series, members = _segment_composite_series(cur, [seg], cal)
                            lv0, lv1 = series.get(cal[0]), series.get(cal[-1])
                            comp_ret[seg] = _pct(lv1, lv0) if lv0 else None
                            comp_note[seg] = len(members)
                        except Exception as e:      # one bad segment never fails the run
                            comp_ret[seg] = None
                            log.warning("qb_universe_derived composite %s: %s", seg, e)
                except Exception as e:
                    log.error("qb_universe_derived: composites unavailable: %s", e)

            ret_1y = {sym: _pct(v[0], v[3]) for sym, v in asof.items()}
            rs = percent_ranks({s: ret_1y.get(s) for s in syms})

            rows = []
            for sym in syms:
                v = asof.get(sym) or (None,) * 5
                c_now, c_3m, c_6m, c_1y, c_3y = v
                r1y = ret_1y.get(sym)
                r3y = _pct(c_now, c_3y)
                seg = seg_of.get(sym)
                b = beta.get(sym)
                row = {"symbol": sym, "score_date": score_date, "price_date": d,
                       "yoy_sales_growth": None, "yoy_profit_growth": None, "opm_latest_quarter": None,
                       "opm_change_yoy": None, "consecutive_quarters_profit_growth": None,
                       "quarters_since_last_result": None, "latest_quarter_end": None,
                       "alpha_vs_nifty500_1y": _r(r1y - bench_1y) if r1y is not None and bench_1y is not None else None,
                       "alpha_vs_nifty500_3y": _r(r3y - bench_3y) if r3y is not None and bench_3y is not None else None,
                       "alpha_vs_sector_1y": (_r(r1y - comp_ret[seg]) if r1y is not None and seg in comp_ret
                                              and comp_ret[seg] is not None else None),
                       "beta_1y": _r(_f(b[0]), 3) if b else None,
                       "beta_sessions": b[1] if b else None,
                       "beta_asof": b[2] if b else None,
                       "max_drawdown_1y": _r(mdd.get(sym)),
                       "rs_percentile_1y": rs.get(sym),
                       "ret_3m": _r(_pct(c_now, c_3m)),
                       "ret_6m": _r(_pct(c_now, c_6m)),
                       "consecutive_up_months": consecutive_up_months(months.get(sym, []), d) if months.get(sym) else None}
                row.update(cat5.get(sym, {}))
                rows.append(row)
            cur.executemany(UPSERT_SQL, rows)
            conn.commit()
        res = {"status": "ok", "score_date": str(score_date), "price_date": str(d), "rows": len(rows),
               "quarterly_symbols": len(cat5), "beta_symbols": len(beta), "segments": len(comp_ret),
               "benchmark_1y": _r(bench_1y), "benchmark_3y": _r(bench_3y), "cal_days": len(cal),
               "duration_s": round(time.time() - t0, 1)}
        log.info("qb_universe_derived: %s", res)
        return res
    finally:
        if own:
            conn.close()


# ── status + manual trigger ─────────────────────────────────────────────────────────────────────
_run_lock = threading.Lock()
_last_run: dict = {"state": "never in this process"}


def _check_admin(token):
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN not configured")
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "bad admin token")


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def in_market_hours(now=None) -> bool:
    now = now or _ist_now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= hm <= 15 * 60 + 30


@router.get("/api/qb/universe2/derived/status")
def derived_status():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT to_regclass('public.{TABLE}')")
        if cur.fetchone()[0] is None:
            return {"status": "no_table", "last_run": _last_run}
        cur.execute(f"SELECT MAX(score_date), MIN(score_date), COUNT(DISTINCT score_date) FROM {TABLE}")
        mx, mn, nd = cur.fetchone()
        if mx is None:
            return {"status": "empty", "last_run": _last_run}
        cur.execute(f"""SELECT COUNT(*), COUNT(latest_quarter_end), COUNT(beta_1y), COUNT(alpha_vs_sector_1y),
                               COUNT(ret_3m), MAX(computed_at), MAX(price_date) FROM {TABLE} WHERE score_date = %s""", (mx,))
        n, nq, nb, na, n3, at, pd_ = cur.fetchone()
    return {"status": "ok", "score_date": str(mx), "first_score_date": str(mn), "dates_stored": nd,
            "rows": n, "quarterly_rows": nq, "beta_rows": nb, "sector_alpha_rows": na, "ret_3m_rows": n3,
            "price_date": str(pd_) if pd_ else None, "computed_at": str(at), "last_run": _last_run}


def _bg_run(force):
    global _last_run
    _last_run = {"state": "running", "started_at": datetime.utcnow().isoformat() + "Z"}
    try:
        _last_run = {"state": "done", **run(force=force)}
    except Exception as e:
        log.error("qb_universe_derived run failed: %s", e, exc_info=True)
        _last_run = {"state": "error", "error": str(e)[:300]}
    finally:
        _run_lock.release()


@router.post("/api/admin/run_qb_universe_derived")
def run_qb_universe_derived_endpoint(force: bool = False, x_admin_token: Optional[str] = Header(None)):
    """Manual trigger (admin token) -- outside market hours only, because the ruling keeps this
    computation off the session. Runs in a background thread; poll /api/qb/universe2/derived/status."""
    _check_admin(x_admin_token)
    if in_market_hours():
        return {"status": "refused", "note": "market hours (09:15-15:30 IST) -- the precompute runs in the night window only"}
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running", "last_run": _last_run}
    threading.Thread(target=_bg_run, args=(force,), name="qb_universe_derived", daemon=True).start()
    return {"status": "started", "poll": "/api/qb/universe2/derived/status"}
