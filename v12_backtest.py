"""
v12_backtest.py — cc#394 V12 Basket Builder, MODULE 3: portfolio backtest walker (spec id=2970
component_4). Async thread + poll; results cached by params_hash in v12_backtests.

MODEL: rotation basket (portfolio walk, NOT per-trade episodes — that is V8). On each rebalance
date we build the as-of universe, rank it by ROC momentum, hold the top-X (subject to optional
RSI/EMA gates + min/max stocks), equal-weight, and between rebalances apply the optional exits
(trailing-peak fall, rank-fall, gate mirror) evaluated at the next rebalance EOD (no intraday in P1).
Equity is walked DAILY on the benchmark trading calendar so drawdown/Sharpe are honest.

POINT-IN-TIME (spec fix_4 + honesty doctrine): momentum/ROC + RSI/EMA gates come from raw_prices,
which ARE point-in-time. Fundamental/GVM universe filters currently resolve against the CURRENT
snapshot (gvm_ft / as-of fundamentals land with Phases 2-4) — so the result is flagged
PIT_PARTIAL and the front end must show that badge. Frozen universes are exact.

The shared basket-definition validator + universe vocabulary live in v12_endpoints (single source).
"""
import os
import json
import bisect
import hashlib
import threading
from typing import Optional
from datetime import date, datetime, timedelta

import psycopg
from fastapi import APIRouter
from pydantic import BaseModel

from v12_endpoints import _uni_where, _UNI_BASE, _validate_basket_def   # single vocabulary + validator

router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(_DB)


_LOOKBACK_DAYS = {"1M": 30, "3M": 91, "6M": 182, "12M": 365}
_FREQ_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 91}
_BENCH = {"NIFTY50": "NIFTY50", "NIFTY100": "NIFTY100", "NIFTY200": "NIFTY200", "NIFTY500": "NIFTY500"}


def params_hash(basket_def, start, end, benchmark):
    raw = json.dumps({"d": basket_def, "s": str(start), "e": str(end), "b": benchmark}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ── point-in-time price series with bisect as-of lookup ──────────────────────────

class _Series:
    """Per-symbol sorted (dates, closes) with an as-of (<=) lookup."""
    def __init__(self):
        self.map = {}

    def add(self, sym, rows):
        ds = [r[0] for r in rows]
        cs = [r[1] for r in rows]
        self.map[sym] = (ds, cs)

    def as_of(self, sym, d):
        pair = self.map.get(sym)
        if not pair or not pair[0]:
            return None
        ds, cs = pair
        i = bisect.bisect_right(ds, d) - 1
        return cs[i] if i >= 0 else None

    def has(self, sym):
        return sym in self.map and bool(self.map[sym][0])


def _load_series(cur, symbols, start, end):
    s = _Series()
    if not symbols:
        return s
    cur.execute("""SELECT symbol, price_date, close FROM raw_prices
                   WHERE symbol = ANY(%s) AND price_date BETWEEN %s AND %s AND close IS NOT NULL
                   ORDER BY symbol, price_date""", (list(symbols), start, end))
    cur_sym, buf = None, []
    for sym, d, c in cur.fetchall():
        if sym != cur_sym:
            if cur_sym is not None:
                s.add(cur_sym, buf)
            cur_sym, buf = sym, []
        buf.append((d, float(c)))
    if cur_sym is not None:
        s.add(cur_sym, buf)
    return s


def _resolve_universe(cur, universe_ref):
    """(symbols, pit_flag). Frozen universes are exact; filtered ones resolve against the current
    snapshot (PIT_PARTIAL until gvm_ft / as-of fundamentals land)."""
    filters = None
    if isinstance(universe_ref, int):
        cur.execute("SELECT definition FROM v12_universes WHERE id=%s", (universe_ref,))
        r = cur.fetchone()
        if not r:
            return [], "universe_not_found"
        defn = r[0] or {}
        if defn.get("frozen_symbols"):
            return [s.upper() for s in defn["frozen_symbols"]], "frozen"
        filters = defn.get("filters", {})
    elif isinstance(universe_ref, dict):
        filters = universe_ref.get("filters", universe_ref)
    else:
        return [], "bad_universe_ref"
    where, params, _ = _uni_where(filters or {})
    cur.execute("SELECT g.symbol " + _UNI_BASE + where + " ORDER BY g.gvm_score DESC NULLS LAST", params)
    return [r[0].upper() for r in cur.fetchall()], "current_snapshot"


def _rebalance_dates(cal, start, end, freq):
    """Rebalance dates snapped onto the benchmark trading calendar `cal` (sorted list of dates)."""
    step = _FREQ_DAYS.get(freq, 30)
    out, d = [], start
    while d <= end:
        i = bisect.bisect_right(cal, d) - 1
        if i >= 0:
            td = cal[i]
            if not out or td != out[-1]:
                out.append(td)
        d = d + timedelta(days=step)
    # always include the last available calendar day as a final mark
    if cal and (not out or out[-1] != cal[-1]):
        out.append(cal[-1])
    return out


def _roc(series, sym, d, months):
    p_now = series.as_of(sym, d)
    p_then = series.as_of(sym, d - timedelta(days=_LOOKBACK_DAYS.get(months, 91)))
    if p_now is None or p_then in (None, 0):
        return None
    return p_now / p_then - 1.0


def _blend_roc(series, sym, d, roc_def):
    if isinstance(roc_def, str):
        return _roc(series, sym, d, roc_def)
    # blend list of {lookback, weight}
    num = wsum = 0.0
    for item in roc_def:
        r = _roc(series, sym, d, item.get("lookback"))
        if r is None:
            return None
        w = float(item.get("weight", 1))
        num += r * w
        wsum += w
    return (num / wsum) if wsum else None


def run_backtest(basket_def, start, end, benchmark="NIFTY50"):
    """Portfolio walk. Returns a result dict (equity series, rebalances, trades, core stats).
    Module 4 enriches the stats pack; this module guarantees the series + trade log + benchmark."""
    entry = basket_def.get("entry", {})
    exit_def = basket_def.get("exit", {}) or {}
    rebal = basket_def.get("rebalance", {}) or {}
    costs = basket_def.get("costs", {}) or {}
    freq = rebal.get("freq", "monthly")
    top_x = int(entry.get("top_x") or 10)
    min_stocks = int(entry.get("min_stocks") or 1)
    max_stocks = int(entry.get("max_stocks") or top_x)
    manual = entry.get("manual_list")
    roc_def = entry.get("roc_lookback", "3M")
    txn = float(costs.get("txn_pct", 0)) / 100.0
    slip = float(costs.get("slippage_pct", 0)) / 100.0
    cost_rate = txn + slip
    trail = exit_def.get("trailing_peak_pct")
    rank_fall_y = exit_def.get("rank_fall_y")

    bench_sym = _BENCH.get(str(benchmark).upper(), "NIFTY50")

    with _conn() as conn, conn.cursor() as cur:
        universe, pit_flag = _resolve_universe(cur, basket_def.get("universe_ref"))
        if manual:
            universe = [s.upper() for s in manual]
            pit_flag = "manual_list"
        if not universe:
            return {"error": "empty universe", "pit_flag": pit_flag}
        # benchmark trading calendar
        cur.execute("""SELECT price_date, close FROM raw_prices
                       WHERE symbol=%s AND price_date BETWEEN %s AND %s AND close IS NOT NULL
                       ORDER BY price_date""", (bench_sym, start, end))
        bench_rows = [(d, float(c)) for d, c in cur.fetchall()]
        if len(bench_rows) < 30:
            return {"error": f"insufficient benchmark history for {bench_sym}", "pit_flag": pit_flag}
        cal = [d for d, _ in bench_rows]
        bench_close = {d: c for d, c in bench_rows}
        # load prices with a lookback buffer so ROC on the first rebalance has history
        load_start = start - timedelta(days=400)
        series = _load_series(cur, universe, load_start, end)

    rebals = _rebalance_dates(cal, start, end, freq)
    if len(rebals) < 2:
        return {"error": "date range too short for the chosen rebalance frequency", "pit_flag": pit_flag}

    # ── walk ──
    equity = 100.0
    peak_since_entry = {}       # symbol -> peak close while held
    holdings = {}               # symbol -> weight
    equity_series, bench_series = [], []
    rebalance_log, trades = [], []
    open_pos = {}               # symbol -> {entry_date, entry_px, weight}
    bench0 = bench_close[cal[bisect.bisect_left(cal, rebals[0])]] if rebals[0] in bench_close else bench_rows[0][1]

    rebal_set = set(rebals)
    start_i = bisect.bisect_left(cal, rebals[0])
    prev_day = cal[start_i]
    equity_series.append({"date": str(prev_day), "equity": round(equity, 4)})
    bench_series.append({"date": str(prev_day), "equity": 100.0})

    def _rank_universe(d):
        scored = []
        for sym in universe:
            r = _blend_roc(series, sym, d, roc_def)
            if r is None:
                continue
            if not _passes_gates(series, sym, d, entry):
                continue
            scored.append((sym, r))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    # initial rebalance
    def _do_rebalance(d):
        nonlocal holdings, equity
        scored = _rank_universe(d)
        ranks = {sym: i + 1 for i, (sym, _) in enumerate(scored)}
        # exits: drop holdings that fell out of rank_fall_y, breached trailing peak, or lost gate
        keep = {}
        for sym, w in holdings.items():
            px = series.as_of(sym, d)
            drop = False
            if rank_fall_y and ranks.get(sym, 10 ** 9) > int(rank_fall_y):
                drop = True
            if trail and px is not None:
                pk = peak_since_entry.get(sym, px)
                if px <= pk * (1 - float(trail) / 100.0):
                    drop = True
            if not drop:
                keep[sym] = w
        # target = top_x names (respect min/max), preferring already-held to reduce turnover
        target = [s for s, _ in scored[:max(top_x, 0)]]
        if len(target) < min_stocks:
            target = [s for s, _ in scored[:min_stocks]]
        target = target[:max_stocks]
        tw = (1.0 / len(target)) if target else 0.0
        new_holdings = {s: tw for s in target}
        # turnover cost: sum of |new - old| weights / 2 (one-way) * cost_rate
        allk = set(new_holdings) | set(holdings)
        turnover = sum(abs(new_holdings.get(s, 0) - holdings.get(s, 0)) for s in allk) / 2.0
        equity *= (1 - turnover * cost_rate)
        # trade log: closes (in holdings, not in new) + opens (in new, not held)
        for s in list(holdings):
            if s not in new_holdings and s in open_pos:
                op = open_pos.pop(s)
                ex = series.as_of(s, d)
                if op["entry_px"] and ex:
                    trades.append({"symbol": s, "entry_date": op["entry_date"], "exit_date": str(d),
                                   "entry_px": round(op["entry_px"], 2), "exit_px": round(ex, 2),
                                   "return_pct": round((ex / op["entry_px"] - 1) * 100, 2)})
                peak_since_entry.pop(s, None)
        for s in new_holdings:
            if s not in open_pos:
                ep = series.as_of(s, d)
                open_pos[s] = {"entry_date": str(d), "entry_px": ep, "weight": tw}
                peak_since_entry[s] = ep if ep else 0
        holdings = new_holdings
        rebalance_log.append({"date": str(d), "n": len(target), "turnover": round(turnover, 4),
                              "holdings": target})

    _do_rebalance(rebals[0])

    # daily walk
    for i in range(start_i + 1, len(cal)):
        d = cal[i]
        # daily portfolio return from held names
        ret = 0.0
        for sym, w in holdings.items():
            pc = series.as_of(sym, d)
            pp = series.as_of(sym, prev_day)
            if pc is not None and pp:
                ret += w * (pc / pp - 1.0)
                if pc > peak_since_entry.get(sym, 0):
                    peak_since_entry[sym] = pc
        equity *= (1 + ret)
        prev_day = d
        if d in rebal_set:
            _do_rebalance(d)
        equity_series.append({"date": str(d), "equity": round(equity, 4)})
        bench_series.append({"date": str(d), "equity": round(bench_close[d] / bench0 * 100, 4)})

    # core stats (Module 4 enriches)
    years = max((cal[-1] - cal[start_i]).days / 365.25, 1e-9)
    total_ret = equity / 100.0 - 1.0
    cagr = (equity / 100.0) ** (1 / years) - 1.0
    peak, maxdd = 0.0, 0.0
    for pt in equity_series:
        peak = max(peak, pt["equity"])
        if peak > 0:
            maxdd = min(maxdd, pt["equity"] / peak - 1.0)
    bench_ret = bench_series[-1]["equity"] / 100.0 - 1.0
    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] < 0]

    return {
        "pit_flag": pit_flag,
        "pit_partial": pit_flag in ("current_snapshot",),
        "universe_size": len(universe), "benchmark": bench_sym,
        "start": str(cal[start_i]), "end": str(cal[-1]), "years": round(years, 2),
        "rebalances": len(rebals), "freq": freq,
        "equity_series": equity_series, "benchmark_series": bench_series,
        "rebalance_log": rebalance_log[-60:], "trades": trades,
        "stats": {
            "start_capital": 100.0, "end_capital": round(equity, 2),
            "absolute_return_pct": round(total_ret * 100, 2), "cagr_pct": round(cagr * 100, 2),
            "max_drawdown_pct": round(maxdd * 100, 2),
            "benchmark_return_pct": round(bench_ret * 100, 2),
            "alpha_pct": round((total_ret - bench_ret) * 100, 2),
            "total_trades": len(trades), "win_trades": len(wins), "loss_trades": len(losses),
            "accuracy_pct": round(len(wins) / (len(wins) + len(losses)) * 100, 1) if (wins or losses) else None,
        },
    }


def _passes_gates(series, sym, d, entry):
    """Optional RSI / EMA gates from raw_prices (point-in-time). Absent gate -> pass."""
    rsi_g = entry.get("rsi_gate")
    ema_g = entry.get("ema_gate")
    if not rsi_g and not ema_g:
        return True
    pair = series.map.get(sym)
    if not pair:
        return False
    ds, cs = pair
    i = bisect.bisect_right(ds, d) - 1
    if i < 20:
        return False
    closes = cs[:i + 1]
    if rsi_g:
        p = int(rsi_g.get("period", 14))
        v = _rsi(closes, p)
        if v is None:
            return False
        thr = float(rsi_g.get("threshold", 50))
        if rsi_g.get("dir") == "above" and not v >= thr:
            return False
        if rsi_g.get("dir") == "below" and not v <= thr:
            return False
    if ema_g:
        e1 = _ema(closes, int(ema_g.get("ema1", 20)))
        e2 = _ema(closes, int(ema_g.get("ema2", 50)))
        if e1 is None or e2 is None or not e1 > e2:
            return False
        if ema_g.get("ema3"):
            e3 = _ema(closes, int(ema_g["ema3"]))
            if e3 is None or not e2 > e3:
                return False
    return True


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / period, losses / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1 + rs)


def _ema(closes, period):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


# ── async job + poll + params_hash cache ─────────────────────────────────────────

def _run_and_store(bt_id, basket_def, start, end, benchmark):
    try:
        res = run_backtest(basket_def, start, end, benchmark)
        with _conn() as conn, conn.cursor() as cur:
            if res.get("error"):
                cur.execute("UPDATE v12_backtests SET status='error', error=%s, finished_at=NOW() WHERE id=%s",
                            (res["error"][:400], bt_id))
            else:
                cur.execute("UPDATE v12_backtests SET status='done', result=%s::jsonb, finished_at=NOW() WHERE id=%s",
                            (json.dumps(res, default=str), bt_id))
            conn.commit()
    except Exception as e:
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("UPDATE v12_backtests SET status='error', error=%s, finished_at=NOW() WHERE id=%s",
                            (str(e)[:400], bt_id))
                conn.commit()
        except Exception:
            pass


class V12BacktestReq(BaseModel):
    basket_id: Optional[int] = None
    definition: Optional[dict] = None
    start: Optional[str] = None
    end: Optional[str] = None
    benchmark: str = "NIFTY50"


@router.post("/api/v12/backtest")
def v12_backtest_run(body: V12BacktestReq):
    """Kick (or return cached) a backtest. Resolves the definition from basket_id or inline body;
    validates; caches by params_hash. Returns {status, backtest_id} — poll /api/v12/backtest/{id}."""
    basket_def = body.definition
    with _conn() as conn, conn.cursor() as cur:
        if basket_def is None and body.basket_id is not None:
            cur.execute("SELECT definition FROM v12_baskets WHERE id=%s", (body.basket_id,))
            r = cur.fetchone()
            if not r:
                return {"error": "basket not found"}
            basket_def = r[0]
        if not basket_def:
            return {"error": "definition or basket_id required"}
        errs = _validate_basket_def(basket_def)
        if errs:
            return {"error": "invalid definition", "errors": errs}
        end = datetime.strptime(body.end, "%Y-%m-%d").date() if body.end else date.today()
        start = datetime.strptime(body.start, "%Y-%m-%d").date() if body.start else (end - timedelta(days=5 * 365))
        ph = params_hash(basket_def, start, end, body.benchmark)
        cur.execute("SELECT id, status, result FROM v12_backtests WHERE params_hash=%s", (ph,))
        row = cur.fetchone()
        if row and row[1] == "done":
            return {"status": "done", "backtest_id": row[0], "cached": True}
        if row and row[1] in ("pending", "running"):
            return {"status": row[1], "backtest_id": row[0], "cached": True}
        cur.execute("INSERT INTO v12_backtests (basket_id, params_hash, status, created_at) "
                    "VALUES (%s, %s, 'running', NOW()) ON CONFLICT (params_hash) DO UPDATE SET status='running' "
                    "RETURNING id", (body.basket_id, ph))
        bt_id = cur.fetchone()[0]
        conn.commit()
    threading.Thread(target=_run_and_store, args=(bt_id, basket_def, start, end, body.benchmark),
                     name=f"v12-bt-{bt_id}", daemon=True).start()
    return {"status": "running", "backtest_id": bt_id}


@router.get("/api/v12/backtest/{bt_id}")
def v12_backtest_get(bt_id: int):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, basket_id, status, result, error, created_at, finished_at "
                    "FROM v12_backtests WHERE id=%s", (bt_id,))
        r = cur.fetchone()
        if not r:
            return {"error": "not found"}
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))
