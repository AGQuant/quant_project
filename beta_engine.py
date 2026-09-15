"""
beta_engine.py -- cc#2032 DATA+ENGINE: nightly per-stock Beta (EOD) + Quant Basket-level Beta
rollup.

Founder request 13-Sep-2026: no beta figure exists anywhere in the schema (confirmed: zero
columns named *beta* across the whole DB before this card). Built under explicit founder
authorization (Fable unavailable, session-standing "clear the queue push all to main, founder
decision").

DEFINITION (founder-proposed, stated so a retune is a number change, not a redesign):
  benchmark:    NIFTY50 (raw_prices symbol) -- the SAME spot series option_ivp.py/deriv_metrics.py
                already read for NIFTY. Reused, not a second index source.
  window:       trailing 252 trading SESSIONS (~1 year) -- the benchmark's own raw_prices trading
                dates define "session", exactly the convention qb_nav.py's compute_series() already
                uses ("The trading calendar is the benchmark's own bar dates"). Not a calendar-day
                window -- a stock missing some interior days still measures its return correctly
                over whatever span it actually has, both legs computed over the identical date pair
                (see _aligned_log_returns).
  min_sessions: 60 -- mirrors the MIN_SESSIONS floor already standing in option_ivp.py (G1/G2):
                never fabricate a beta off too little history. Below this: no row in beta_daily
                for that symbol that date, not a zeroed or guessed value.
  stock_beta:   Cov(r_stock, r_bench) / Var(r_bench), r = daily log returns over the aligned window.
  basket_beta:  holdings-weighted average of each CURRENT holding's own stock beta, weight = that
                holding's current market value (quant_paper_positions.current_value) as a fraction
                of the basket's total open-position value -- the SAME weight quant basket surfaces
                already compute at read time (qb_app_mobile.py's own weight_pct: current_value /
                sum(current_value)), not a second weighting scheme. NIFTYBEES/LIQUIDBEES (the
                established cash-parking sleeve, qb_nav.py's CASH_PARK_SYMBOLS) are excluded from
                the rollup entirely -- they are not real equity market exposure, the same reason
                qb_nav.py marks them at cost rather than pricing them. A holding with no computable
                beta (new listing, <60 sessions) is excluded from BOTH the numerator and denominator
                of the weighted average -- restricting the weighted-average sum to the subset that
                HAS a beta, using each one's own (already-normalized) weight, is mathematically
                exactly "redistribute its weight proportionally across the rest" -- never a
                fabricated beta=1 or beta=0 for the excluded holding.

STORAGE -- two NEW tables, no ALTER TABLE anywhere (MAINTENANCE_LOCK_RULE, cc#351: ALTER TABLE is
  Railway-console-only/weekends/propose-first; a CREATE TABLE is not gated). The card's spec
  offered a choice for the basket-level number -- a new qb_nav_daily COLUMN, or a new small table.
  CC's call, stated here: a new table (qb_beta_daily), specifically to avoid an ALTER TABLE on
  qb_nav_daily, a live table other nightly jobs write every session. Per-stock beta is similarly
  wired into quant_basket-surface responses at READ TIME (a LEFT JOIN on (symbol, latest d)) rather
  than added as a new quant_paper_positions column, for the identical reason.
  beta_daily(symbol, d, beta, n_sessions, benchmark_sym, computed_at) PK(symbol, d)
  qb_beta_daily(basket_name, nav_date, beta, n_holdings_used, n_holdings_excluded, computed_at)
    PK(basket_name, nav_date)

SCHEDULING -- registers via the SAME AST-derived enumeration every other scheduler.py job uses
  (scheduler_master.py's code-enumeration + drift audit auto-inserts a job it finds `_spawn()`ed
  in scheduler.py; ENGINE_LIVENESS_RULE 13829: registry-derived, never a hand-typed INSERT that
  could drift from the code). Scheduled 01:20 IST -- AFTER raw_prices' EOD close lands (01:00,
  _bg_yahoo_daily_sync) and AFTER quant_paper_positions.current_value is marked fresh by the
  nightly QB pass (01:15, _bg_qb_eod), BEFORE the 01:30 GVM recompute -- same "prices->QB->GVM"
  dependency order scheduler.py's own comment already documents for that slot.

TRIGGER for a first real run inside this session (sandbox has no HTTP path to prod, same pattern
  as bhavcopy_diagnostic.py / option_iv_history.py / gvm_history_pit_backfill.py): app_config flag
  'beta_engine_run'='run', claimed atomically on deploy startup, runs in a daemon thread.

AMENDED cc#2033 (15-Sep-2026): basket-list card GVM. compute_all_basket_gvm() reuses this file's
  own _basket_weights() (cc#2032's original per-basket weighting, extracted unchanged so beta and
  GVM cannot drift onto two different weighting paths), scored against gvm_scores (the live
  current-snapshot GVM table). A genuine data-source correction found before building: the card's
  spec named quant_basket.gvm_score as the source -- that table is EMPTY (0 rows) in production;
  gvm_scores is the real one, confirmed by checking real held-symbol coverage (checked: all but
  three ETF holdings, which are not scored equities, have a real score). Own gated trigger
  ('basket_gvm_run'), own table (qb_gvm_daily, a sibling of qb_beta_daily rather than a column on
  it -- qb_beta_daily already has real production data and MAINTENANCE_LOCK_RULE gates ALTER
  TABLE), own scheduler.py slot AFTER the 01:30 GVM recompute (unlike beta, which runs at 01:20
  and needs no gvm_scores dependency).
"""
import logging
import math
import os
import threading
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np
import psycopg
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.beta_engine")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
router = APIRouter(tags=["beta-engine"])

BENCHMARK_SYM = "NIFTY50"
WINDOW_SESSIONS = 252          # PROPOSED, founder can retune -- the floor below never moves
MIN_SESSIONS = 60              # RULED floor, mirrors option_ivp.py G1/G2 -- never fabricate
CASH_PARK_SYMBOLS = ("NIFTYBEES", "LIQUIDBEES")   # qb_nav.py's own convention, reused here
FLAG_KEY = "beta_engine_run"
_running = False


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS beta_daily (
        symbol TEXT NOT NULL, d DATE NOT NULL, beta NUMERIC, n_sessions INT,
        benchmark_sym TEXT, computed_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (symbol, d))""")
    cur.execute("""CREATE INDEX IF NOT EXISTS beta_daily_d_idx ON beta_daily (d)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS qb_beta_daily (
        basket_name TEXT NOT NULL, nav_date DATE NOT NULL, beta NUMERIC,
        n_holdings_used INT, n_holdings_excluded INT, computed_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (basket_name, nav_date))""")
    # cc#2033: sibling table, not a qb_beta_daily column -- qb_beta_daily is itself a table CC
    # created minutes earlier this same session with one real write already in it; an ALTER TABLE
    # to add a gvm column would hit the same MAINTENANCE_LOCK_RULE gate cc#2032's own report cited
    # for qb_nav_daily, so the two stay sibling tables sharing (basket_name, nav_date), joined at
    # read time -- see performance_endpoints.py / qb_app_mobile.py.
    cur.execute("""CREATE TABLE IF NOT EXISTS qb_gvm_daily (
        basket_name TEXT NOT NULL, nav_date DATE NOT NULL, gvm NUMERIC,
        n_holdings_used INT, n_holdings_excluded INT, computed_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (basket_name, nav_date))""")


def _aligned_log_returns(bench_by_date: Dict[str, float], stock_by_date: Dict[str, float],
                          ordered_dates: List[str]) -> Tuple[np.ndarray, np.ndarray]:
    """Both return series computed over the EXACT SAME date pairs -- dates where the STOCK has a
    positive close, walked in the benchmark's own calendar order. A stock missing an interior
    session (halt, thin listing) simply spans that gap in ONE return observation, and the
    benchmark's matching return is measured over the identical span -- never forward-filled (a
    filled day would read as a fake zero return and bias beta toward 0), never misaligned (the
    benchmark leg is re-sampled at the stock's own available dates, not its own full calendar)."""
    common = [d for d in ordered_dates if stock_by_date.get(d, 0) and stock_by_date[d] > 0]
    if len(common) < 2:
        return np.array([]), np.array([])
    s_px = np.array([stock_by_date[d] for d in common], dtype=float)
    b_px = np.array([bench_by_date[d] for d in common], dtype=float)
    return np.diff(np.log(s_px)), np.diff(np.log(b_px))


def _beta_from_returns(s_ret: np.ndarray, b_ret: np.ndarray) -> Optional[float]:
    if len(s_ret) < MIN_SESSIONS:
        return None
    var_b = float(np.var(b_ret, ddof=1))
    if var_b <= 0 or not math.isfinite(var_b):
        return None
    cov_sb = float(np.cov(s_ret, b_ret, ddof=1)[0, 1])
    if not math.isfinite(cov_sb):
        return None
    return cov_sb / var_b


def compute_all_stock_betas(cur, as_of: Optional[date] = None) -> Dict:
    """For every symbol in raw_prices with >=MIN_SESSIONS overlapping sessions vs NIFTY50, compute
    beta and upsert into beta_daily for `as_of` (default: the latest raw_prices date). ONE bulk
    query for the whole universe's closes (~1850 symbols), not 1850 round trips -- same "one read
    for the whole set" discipline qb_nav.py's _closes() already established.

    Returns coverage stats for the first-run report: n_full (a complete 252-session window), n_partial
    (60-251 sessions, e.g. a newer listing), n_excluded (<60 sessions -- present in raw_prices but
    too new/thin to score, listed explicitly rather than silently absent)."""
    if as_of is None:
        cur.execute("SELECT MAX(price_date) FROM raw_prices")
        r = cur.fetchone()
        as_of = r[0] if r and r[0] else None
    if as_of is None:
        return {"ok": False, "error": "raw_prices is empty"}

    cur.execute("""SELECT price_date, close FROM raw_prices
                   WHERE symbol = %s AND price_date <= %s AND close > 0
                   ORDER BY price_date DESC LIMIT %s""", (BENCHMARK_SYM, as_of, WINDOW_SESSIONS))
    bench_rows = list(reversed(cur.fetchall()))
    if len(bench_rows) < MIN_SESSIONS + 1:
        return {"ok": False, "error": f"only {len(bench_rows)} {BENCHMARK_SYM} sessions <= {as_of}, "
                                       f"need {MIN_SESSIONS + 1}+"}
    bench_dates = [str(r[0]) for r in bench_rows]
    bench_by_date = {str(r[0]): float(r[1]) for r in bench_rows}
    window_start, window_end = bench_dates[0], bench_dates[-1]

    cur.execute("""SELECT symbol, price_date, close FROM raw_prices
                   WHERE price_date >= %s AND price_date <= %s AND close > 0
                   ORDER BY symbol, price_date""", (window_start, window_end))
    by_symbol: Dict[str, Dict[str, float]] = {}
    for sym, d, close in cur.fetchall():
        by_symbol.setdefault(sym, {})[str(d)] = float(close)

    n_full, n_partial, n_excluded = 0, 0, 0
    excluded_examples, rows = [], []
    for sym, closes in by_symbol.items():
        s_ret, b_ret = _aligned_log_returns(bench_by_date, closes, bench_dates)
        n_sessions = len(s_ret)
        beta = _beta_from_returns(s_ret, b_ret)
        if beta is None:
            n_excluded += 1
            if len(excluded_examples) < 10:
                excluded_examples.append({"symbol": sym, "n_sessions": n_sessions})
            continue
        if n_sessions >= WINDOW_SESSIONS - 1:
            n_full += 1
        else:
            n_partial += 1
        rows.append((sym, as_of, round(beta, 4), n_sessions, BENCHMARK_SYM))

    for sym, d, beta, n_sessions, bench_sym in rows:
        cur.execute("""INSERT INTO beta_daily (symbol, d, beta, n_sessions, benchmark_sym, computed_at)
                       VALUES (%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (symbol, d) DO UPDATE SET
                         beta=EXCLUDED.beta, n_sessions=EXCLUDED.n_sessions,
                         benchmark_sym=EXCLUDED.benchmark_sym, computed_at=NOW()""",
                    (sym, d, beta, n_sessions, bench_sym))

    return {"ok": True, "as_of": str(as_of), "universe_size": len(by_symbol),
            "rows_written": len(rows), "n_full_window": n_full, "n_partial_window": n_partial,
            "n_excluded": n_excluded, "excluded_examples": excluded_examples,
            "window_start": window_start, "window_end": window_end}


def _active_baskets(cur) -> List[str]:
    """Registry-derived (quant_basket_registry.is_active), never a hardcoded basket list --
    the ONE enumeration both compute_all_basket_betas and compute_all_basket_gvm (cc#2033) use."""
    cur.execute("SELECT basket_name FROM quant_basket_registry WHERE is_active=TRUE ORDER BY basket_name")
    return [r[0] for r in cur.fetchall()]


def _basket_weights(cur, basket_name: str) -> List[Tuple[str, float]]:
    """[(symbol, weight)] for every CURRENTLY OPEN, non-cash-park position in this basket --
    weight = current_value / total open non-cash-park value, the SAME weight
    qb_app_mobile.py's own weight_pct already computes at read time. THE ONE weight computation
    cc#2032's basket_beta and cc#2033's basket_gvm both use (cc#2033's own explicit instruction:
    "identical weighting source/logic... do not invent a second weighting path") -- extracted
    here so the two rollups cannot silently drift apart; behaviour is byte-for-byte identical to
    cc#2032's original inline version (re-verified after this extraction, see CC2033 report).
    Empty list when the basket has no priced open holdings (all cash-parked, or none)."""
    cur.execute("""SELECT symbol, current_value FROM quant_paper_positions
                   WHERE basket_name=%s AND status='open'""", (basket_name,))
    pos = [(r[0], float(r[1] or 0)) for r in cur.fetchall() if r[0] not in CASH_PARK_SYMBOLS]
    total_value = sum(v for _, v in pos)
    if not pos or total_value <= 0:
        return []
    return [(sym, value / total_value) for sym, value in pos]


def compute_all_basket_betas(cur, as_of: date) -> Dict:
    """Holdings-weighted beta per ACTIVE basket, from that basket's CURRENT open positions and
    today's beta_daily."""
    baskets = _active_baskets(cur)
    results = []
    for name in baskets:
        weights = _basket_weights(cur, name)
        if not weights:
            results.append({"basket": name, "beta": None, "n_holdings_used": 0,
                             "n_holdings_excluded": 0, "reason": "no priced open holdings"})
            continue
        syms = [s for s, _ in weights]
        cur.execute("""SELECT symbol, beta FROM beta_daily
                       WHERE symbol = ANY(%s) AND d = (
                           SELECT MAX(d) FROM beta_daily WHERE symbol = ANY(%s) AND d <= %s)""",
                    (syms, syms, as_of))
        beta_map = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}

        weight_num, weight_den, used, excluded = 0.0, 0.0, 0, 0
        for sym, w in weights:
            b = beta_map.get(sym)
            if b is None:
                excluded += 1
                continue
            weight_num += w * b
            weight_den += w
            used += 1
        basket_beta = round(weight_num / weight_den, 4) if weight_den > 0 else None
        results.append({"basket": name, "beta": basket_beta, "n_holdings_used": used,
                         "n_holdings_excluded": excluded})
        cur.execute("""INSERT INTO qb_beta_daily
                       (basket_name, nav_date, beta, n_holdings_used, n_holdings_excluded, computed_at)
                       VALUES (%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (basket_name, nav_date) DO UPDATE SET
                         beta=EXCLUDED.beta, n_holdings_used=EXCLUDED.n_holdings_used,
                         n_holdings_excluded=EXCLUDED.n_holdings_excluded, computed_at=NOW()""",
                    (name, as_of, basket_beta, used, excluded))
    return {"baskets": len(baskets), "results": results}


def compute_all_basket_gvm(cur, as_of: date) -> Dict:
    """cc#2033: holdings-weighted GVM per ACTIVE basket -- same _basket_weights() as beta above,
    scored against gvm_scores (the LIVE current-snapshot GVM table -- NOT quant_basket.gvm_score,
    which was found EMPTY, 0 rows, before this card: a real data-source correction, not the spec's
    literal source). A holding with no gvm_scores row (an ETF like GOLDBEES/SILVERBEES/MID150BEES,
    which are not scored equities) is excluded from both the numerator and denominator, same
    never-fabricate handling as beta."""
    baskets = _active_baskets(cur)
    results = []
    for name in baskets:
        weights = _basket_weights(cur, name)
        if not weights:
            results.append({"basket": name, "gvm": None, "n_holdings_used": 0,
                             "n_holdings_excluded": 0, "reason": "no priced open holdings"})
            continue
        syms = [s for s, _ in weights]
        cur.execute("""SELECT symbol, gvm_score FROM gvm_scores
                       WHERE symbol = ANY(%s) AND score_date = (
                           SELECT MAX(score_date) FROM gvm_scores WHERE symbol = ANY(%s) AND score_date <= %s)""",
                    (syms, syms, as_of))
        gvm_map = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}

        weight_num, weight_den, used, excluded = 0.0, 0.0, 0, 0
        for sym, w in weights:
            g = gvm_map.get(sym)
            if g is None:
                excluded += 1
                continue
            weight_num += w * g
            weight_den += w
            used += 1
        basket_gvm = round(weight_num / weight_den, 4) if weight_den > 0 else None
        results.append({"basket": name, "gvm": basket_gvm, "n_holdings_used": used,
                         "n_holdings_excluded": excluded})
        cur.execute("""INSERT INTO qb_gvm_daily
                       (basket_name, nav_date, gvm, n_holdings_used, n_holdings_excluded, computed_at)
                       VALUES (%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (basket_name, nav_date) DO UPDATE SET
                         gvm=EXCLUDED.gvm, n_holdings_used=EXCLUDED.n_holdings_used,
                         n_holdings_excluded=EXCLUDED.n_holdings_excluded, computed_at=NOW()""",
                    (name, as_of, basket_gvm, used, excluded))
    return {"baskets": len(baskets), "results": results}


def run_basket_gvm_engine(as_of: Optional[date] = None) -> Dict:
    """cc#2033 nightly pass: ensure tables, compute every active basket's holdings-weighted GVM
    for the latest gvm_scores date (default) or a given as_of. Scheduled AFTER the 01:30 IST GVM
    recompute (scheduler._bg_gvm) so today's gvm_scores are fresh -- see scheduler._bg_basket_gvm.
    Never raises -- caller gets {ok:False, error}."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_tables(cur)
            conn.commit()
            if as_of is None:
                cur.execute("SELECT MAX(score_date) FROM gvm_scores")
                r = cur.fetchone()
                as_of = r[0] if r and r[0] else None
            if as_of is None:
                return {"ok": False, "error": "gvm_scores is empty"}
            gvm_res = compute_all_basket_gvm(cur, as_of)
            conn.commit()
        return {"ok": True, "as_of": str(as_of), "gvm_result": gvm_res}
    except Exception as e:
        log.error(f"basket_gvm run failed: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


def run_beta_engine() -> Dict:
    """Full nightly pass: ensure tables, compute every stock's beta for the latest raw_prices
    date, then every active basket's holdings-weighted rollup from that same date. Never raises --
    caller (scheduler.py's _bg_beta_engine, or the gated trigger below) gets {ok:False, error}."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_tables(cur)
            conn.commit()
            stock_res = compute_all_stock_betas(cur)
            conn.commit()
            if not stock_res.get("ok"):
                return {"ok": False, "stock_result": stock_res}
            as_of = date.fromisoformat(stock_res["as_of"])
            basket_res = compute_all_basket_betas(cur, as_of)
            conn.commit()
        return {"ok": True, "as_of": stock_res["as_of"], "stock_result": stock_res,
                "basket_result": basket_res}
    except Exception as e:
        log.error(f"beta_engine run failed: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


def _run_bg():
    try:
        out = run_beta_engine()
        with _conn() as conn, conn.cursor() as cur:
            import json
            cur.execute("""INSERT INTO app_config(key,value,updated_at) VALUES(%s,%s,NOW())
                           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                        (f"{FLAG_KEY}_result", json.dumps(out, default=str)[:60000]))
            cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (FLAG_KEY,))
            conn.commit()
    except Exception as e:
        log.error(f"beta_engine background run crashed: {e}")
    finally:
        global _running
        _running = False


def _claim_flag() -> bool:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT value FROM app_config WHERE key=%s
                           AND (value='run' OR (value='claimed' AND updated_at < NOW() - INTERVAL '10 minutes'))
                           FOR UPDATE""", (FLAG_KEY,))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s", (FLAG_KEY,))
            conn.commit()
        return r is not None
    except Exception as e:
        log.error(f"beta_engine flag claim failed: {e}")
        return False


def _maybe_start() -> bool:
    global _running
    if _running:
        return False
    _running = True
    threading.Thread(target=_run_bg, name="cc2032-beta-engine", daemon=True).start()
    return True


@router.on_event("startup")
async def _startup_trigger():
    # CC sandbox has no HTTP path to prod (same pattern as bhavcopy_diagnostic.py / option_iv_
    # history.py / gvm_history_pit_backfill.py): set app_config['beta_engine_run']='run' via
    # run_sql, then this deploy's boot claims it atomically and starts the daemon thread.
    if _claim_flag():
        _maybe_start()


@router.post("/api/admin/beta/run")
def beta_run(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    started = _maybe_start()
    return {"started": started, "already_running": not started}


@router.get("/api/admin/beta/status")
def beta_status(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(d) FROM beta_daily")
        n_beta, max_d = cur.fetchone()
        cur.execute("SELECT COUNT(*), MAX(nav_date) FROM qb_beta_daily")
        n_qb, max_nav = cur.fetchone()
    return {"running": _running, "beta_daily_rows": n_beta, "beta_daily_latest": str(max_d) if max_d else None,
            "qb_beta_daily_rows": n_qb, "qb_beta_daily_latest": str(max_nav) if max_nav else None}


# ── cc#2033: basket GVM rollup -- own gated trigger, own flag, INDEPENDENT of beta's above.
# Runs on its own later schedule slot (scheduler._bg_basket_gvm, after the 01:30 GVM recompute),
# so it needs its own claim/thread machinery rather than piggy-backing on beta's -- the two must
# be able to fire on different deploys/times without one blocking the other.
FLAG_KEY_GVM = "basket_gvm_run"
_running_gvm = False


def _run_bg_gvm():
    try:
        out = run_basket_gvm_engine()
        with _conn() as conn, conn.cursor() as cur:
            import json
            cur.execute("""INSERT INTO app_config(key,value,updated_at) VALUES(%s,%s,NOW())
                           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                        (f"{FLAG_KEY_GVM}_result", json.dumps(out, default=str)[:60000]))
            cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (FLAG_KEY_GVM,))
            conn.commit()
    except Exception as e:
        log.error(f"basket_gvm background run crashed: {e}")
    finally:
        global _running_gvm
        _running_gvm = False


def _claim_flag_gvm() -> bool:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT value FROM app_config WHERE key=%s
                           AND (value='run' OR (value='claimed' AND updated_at < NOW() - INTERVAL '10 minutes'))
                           FOR UPDATE""", (FLAG_KEY_GVM,))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s", (FLAG_KEY_GVM,))
            conn.commit()
        return r is not None
    except Exception as e:
        log.error(f"basket_gvm flag claim failed: {e}")
        return False


def _maybe_start_gvm() -> bool:
    global _running_gvm
    if _running_gvm:
        return False
    _running_gvm = True
    threading.Thread(target=_run_bg_gvm, name="cc2033-basket-gvm", daemon=True).start()
    return True


@router.on_event("startup")
async def _startup_trigger_gvm():
    if _claim_flag_gvm():
        _maybe_start_gvm()


@router.post("/api/admin/basket_gvm/run")
def basket_gvm_run(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    started = _maybe_start_gvm()
    return {"started": started, "already_running": not started}


@router.get("/api/admin/basket_gvm/status")
def basket_gvm_status(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(nav_date) FROM qb_gvm_daily")
        n_gvm, max_nav = cur.fetchone()
    return {"running": _running_gvm, "qb_gvm_daily_rows": n_gvm,
            "qb_gvm_daily_latest": str(max_nav) if max_nav else None}
