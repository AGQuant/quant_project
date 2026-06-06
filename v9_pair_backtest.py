"""
V9 Pair Strategy — Backtest Engine
=====================================
Runs parameter combinations on valid pairs discovered by v9_pair_discovery.py.
Uses 18-month EOD closing prices (1-Dec-2024 to 31-May-2026). No look-ahead bias.
Earnings blackout: skipped for backtest (will be added in live engine).

Combos 1-10: original swing set (20-day time stop).
Combos 11-15: fast-reversion set — high Z-entry, short window, 7-day time stop.
Combos 16-17: combo 12 base with extended time stop (10d, 12d).

Each combo may carry its own "time_stop" key; falls back to TIME_STOP_DEFAULT (20).

Output tables: pair_backtest_results, pair_backtest_trades
"""

import os
import logging
import numpy as np
import pandas as pd
from datetime import date
from typing import List, Dict, Optional, Tuple
from scipy import stats

import psycopg2

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('v9_backtest')

DATABASE_URL = os.environ.get('DATABASE_URL')

# ── Locked constants ──────────────────────────────────────────────────────────
BACKTEST_START    = '2024-12-01'
BACKTEST_END      = '2026-05-31'
DISCOVERY_DATE    = '2024-12-01'
TIME_STOP_DEFAULT = 20
REGIME_CORR_MIN   = 0.60
REGIME_WINDOW     = 20


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── Parameter Combinations ────────────────────────────────────────────────────
COMBOS = [
    {"id": 1,  "z_entry": 2.0, "z_exit": 0.5, "z_stop": 3.5, "window": 60, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 2,  "z_entry": 2.0, "z_exit": 0.5, "z_stop": 3.5, "window": 90, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 3,  "z_entry": 2.0, "z_exit": 0.5, "z_stop": 3.5, "window": 30, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 4,  "z_entry": 1.5, "z_exit": 0.5, "z_stop": 3.0, "window": 60, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 5,  "z_entry": 1.5, "z_exit": 0.3, "z_stop": 3.0, "window": 60, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 6,  "z_entry": 2.5, "z_exit": 0.5, "z_stop": 3.5, "window": 60, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 7,  "z_entry": 2.0, "z_exit": 0.0, "z_stop": 3.5, "window": 60, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 8,  "z_entry": 2.0, "z_exit": 0.5, "z_stop": 3.5, "window": 60, "hedge_recompute": "monthly", "time_stop": 20},
    {"id": 9,  "z_entry": 1.5, "z_exit": 0.5, "z_stop": 3.0, "window": 90, "hedge_recompute": "monthly", "time_stop": 20},
    {"id": 10, "z_entry": 2.5, "z_exit": 0.3, "z_stop": 4.0, "window": 90, "hedge_recompute": "weekly",  "time_stop": 20},
    {"id": 11, "z_entry": 3.0, "z_exit": 1.0, "z_stop": 4.0, "window": 20, "hedge_recompute": "weekly",  "time_stop": 7},
    {"id": 12, "z_entry": 2.5, "z_exit": 1.0, "z_stop": 4.0, "window": 15, "hedge_recompute": "weekly",  "time_stop": 7},
    {"id": 13, "z_entry": 3.0, "z_exit": 1.5, "z_stop": 4.5, "window": 20, "hedge_recompute": "weekly",  "time_stop": 7},
    {"id": 14, "z_entry": 2.5, "z_exit": 1.0, "z_stop": 4.0, "window": 10, "hedge_recompute": "weekly",  "time_stop": 7},
    {"id": 15, "z_entry": 3.5, "z_exit": 1.5, "z_stop": 5.0, "window": 15, "hedge_recompute": "weekly",  "time_stop": 7},
    {"id": 16, "z_entry": 2.5, "z_exit": 1.0, "z_stop": 4.0, "window": 15, "hedge_recompute": "weekly",  "time_stop": 10},
    {"id": 17, "z_entry": 2.5, "z_exit": 1.0, "z_stop": 4.0, "window": 15, "hedge_recompute": "weekly",  "time_stop": 12},
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pair_backtest_results (
    id               SERIAL PRIMARY KEY,
    combo_id         INTEGER NOT NULL,
    z_entry          NUMERIC(4,2),
    z_exit           NUMERIC(4,2),
    z_stop           NUMERIC(4,2),
    zscore_window    INTEGER,
    hedge_recompute  TEXT,
    symbol_a         TEXT,
    symbol_b         TEXT,
    segment          TEXT,
    total_trades     INTEGER DEFAULT 0,
    win_trades       INTEGER DEFAULT 0,
    loss_trades      INTEGER DEFAULT 0,
    stop_trades      INTEGER DEFAULT 0,
    time_stop_trades INTEGER DEFAULT 0,
    win_rate         NUMERIC(6,2),
    stop_rate        NUMERIC(6,2),
    time_stop_rate   NUMERIC(6,2),
    total_pnl        NUMERIC(12,2),
    avg_return_pct   NUMERIC(8,4),
    avg_holding_days NUMERIC(6,1),
    max_drawdown     NUMERIC(8,4),
    sharpe_ratio     NUMERIC(8,4),
    profit_factor    NUMERIC(8,4),
    worst_trade      NUMERIC(12,2),
    max_dd_rupees    NUMERIC(12,2),
    backtest_period  TEXT,
    run_at           TIMESTAMP DEFAULT NOW(),
    UNIQUE(combo_id, symbol_a, symbol_b)
);
ALTER TABLE pair_backtest_results ADD COLUMN IF NOT EXISTS worst_trade NUMERIC(12,2);
ALTER TABLE pair_backtest_results ADD COLUMN IF NOT EXISTS max_dd_rupees NUMERIC(12,2);
CREATE INDEX IF NOT EXISTS idx_pbt_combo   ON pair_backtest_results(combo_id);
CREATE INDEX IF NOT EXISTS idx_pbt_segment ON pair_backtest_results(segment);

CREATE TABLE IF NOT EXISTS pair_backtest_trades (
    id            SERIAL PRIMARY KEY,
    combo_id      INTEGER NOT NULL,
    symbol_a      TEXT NOT NULL,
    symbol_b      TEXT NOT NULL,
    direction     TEXT NOT NULL,
    entry_date    DATE,
    exit_date     DATE,
    entry_z       NUMERIC(8,4),
    exit_z        NUMERIC(8,4),
    entry_price_a NUMERIC(10,2),
    entry_price_b NUMERIC(10,2),
    exit_price_a  NUMERIC(10,2),
    exit_price_b  NUMERIC(10,2),
    lot_size_a    INTEGER,
    lot_size_b    INTEGER,
    pnl_a         NUMERIC(10,2),
    pnl_b         NUMERIC(10,2),
    total_pnl     NUMERIC(10,2),
    return_pct    NUMERIC(8,4),
    holding_days  INTEGER,
    exit_reason   TEXT,
    run_at        TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pbt_trades_combo ON pair_backtest_trades(combo_id, symbol_a, symbol_b);
"""


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def load_valid_pairs(conn) -> List[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol_a, symbol_b, segment, hedge_ratio, hedge_intercept,
                   correlation, coint_pvalue
            FROM pair_universe
            WHERE is_active = TRUE AND discovery_date = %s
            ORDER BY segment, correlation DESC
        """, (DISCOVERY_DATE,))
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d['hedge_ratio'] = _f(d['hedge_ratio'])
            out.append(d)
        return out


def load_prices(conn, symbols: List[str]) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, price_date, close::float8 FROM raw_prices
            WHERE symbol = ANY(%s) AND price_date BETWEEN %s AND %s
            ORDER BY price_date ASC
        """, (symbols, BACKTEST_START, BACKTEST_END))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=['symbol', 'price_date', 'close'])
    df['price_date'] = pd.to_datetime(df['price_date'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    return df.pivot(index='price_date', columns='symbol', values='close').astype(float)


def load_lot_sizes(conn, symbols: List[str]) -> Dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, lot_size FROM futures_universe
            WHERE symbol = ANY(%s) AND is_active = TRUE
        """, (symbols,))
        return {r[0]: int(r[1] or 1) for r in cur.fetchall()}


def compute_hedge_ratio(s_a: np.ndarray, s_b: np.ndarray) -> Tuple[float, float]:
    if len(s_a) < 20:
        return 1.0, 0.0
    slope, intercept, _, _, _ = stats.linregress(s_b, s_a)
    return float(slope), float(intercept)


def should_recompute_hedge(current_date, last_recompute, frequency: str) -> bool:
    if last_recompute is None:
        return True
    if frequency == "weekly":
        return (current_date - last_recompute).days >= 7
    return (current_date - last_recompute).days >= 30


def compute_sharpe(returns: List[float]) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    return float((arr.mean() / arr.std()) * np.sqrt(252)) if arr.std() > 0 else 0.0


def compute_max_drawdown(cumulative_pnl: List[float]) -> float:
    if not cumulative_pnl:
        return 0.0
    arr  = np.array(cumulative_pnl)
    peak = np.maximum.accumulate(arr)
    dd   = (arr - peak) / np.where(peak != 0, np.abs(peak), 1)
    return float(dd.min())


def compute_max_dd_rupees(cumulative_pnl: List[float]) -> float:
    if not cumulative_pnl:
        return 0.0
    arr  = np.array(cumulative_pnl)
    peak = np.maximum.accumulate(arr)
    return float((arr - peak).min())


def compute_profit_factor(pnls: List[float]) -> float:
    gains  = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses == 0:
        return 999.0 if gains > 0 else 0.0
    return round(float(gains / losses), 4)


def _calc_pnl(position: dict, exit_price_a: float, exit_price_b: float,
              lot_a: int, lot_b: int) -> Tuple[float, float, float, float]:
    ep_a = float(position['entry_price_a'])
    ep_b = float(position['entry_price_b'])
    exit_price_a = float(exit_price_a)
    exit_price_b = float(exit_price_b)
    if position['direction'] == 'LONG_SPREAD':
        pnl_a = (exit_price_a - ep_a) * lot_a
        pnl_b = (ep_b - exit_price_b) * lot_b
    else:
        pnl_a = (ep_a - exit_price_a) * lot_a
        pnl_b = (exit_price_b - ep_b) * lot_b
    total_pnl = round(float(pnl_a + pnl_b), 2)
    notional  = ep_a * lot_a + ep_b * lot_b
    ret_pct   = round(float(total_pnl / notional * 100), 4) if notional else 0.0
    return round(float(pnl_a), 2), round(float(pnl_b), 2), total_pnl, ret_pct


def _make_trade(position: dict, sym_a: str, sym_b: str,
                exit_date: date, exit_z: float,
                exit_price_a: float, exit_price_b: float,
                lot_a: int, lot_b: int,
                pnl_a: float, pnl_b: float, total_pnl: float,
                ret_pct: float, holding_days: int,
                exit_reason: str, combo_id: int) -> dict:
    return {
        'combo_id':       int(combo_id),
        'symbol_a':       sym_a,
        'symbol_b':       sym_b,
        'direction':      position['direction'],
        'entry_date':     position['entry_date'],
        'exit_date':      exit_date,
        'entry_z':        round(_f(position['entry_z']), 4),
        'exit_z':         round(_f(exit_z), 4),
        'entry_price_a':  round(_f(position['entry_price_a']), 2),
        'entry_price_b':  round(_f(position['entry_price_b']), 2),
        'exit_price_a':   round(_f(exit_price_a), 2),
        'exit_price_b':   round(_f(exit_price_b), 2),
        'lot_size_a':     _i(lot_a),
        'lot_size_b':     _i(lot_b),
        'pnl_a':          _f(pnl_a),
        'pnl_b':          _f(pnl_b),
        'total_pnl':      _f(total_pnl),
        'return_pct':     _f(ret_pct),
        'holding_days':   _i(holding_days),
        'exit_reason':    exit_reason,
    }


def backtest_pair(prices: pd.DataFrame, lot_sizes: Dict[str, int],
                  pair: dict, combo: dict) -> Tuple[Optional[dict], List[dict]]:
    sym_a = pair['symbol_a']
    sym_b = pair['symbol_b']

    if sym_a not in prices.columns or sym_b not in prices.columns:
        return None, []

    aligned = pd.concat([prices[sym_a], prices[sym_b]], axis=1).dropna()
    aligned.columns = ['A', 'B']
    if len(aligned) < combo['window'] + 20:
        return None, []

    dates     = aligned.index
    a_prices  = aligned['A'].values.astype(float)
    b_prices  = aligned['B'].values.astype(float)
    n         = len(dates)
    window    = combo['window']
    time_stop = combo.get('time_stop', TIME_STOP_DEFAULT)
    lot_a     = int(lot_sizes.get(sym_a, 1))
    lot_b     = int(lot_sizes.get(sym_b, 1))

    trades     = []
    position   = None
    beta       = float(pair['hedge_ratio'])
    last_hedge = None
    spreads    = []

    for i in range(window, n):
        today   = dates[i].date()
        price_a = float(a_prices[i])
        price_b = float(b_prices[i])

        if should_recompute_hedge(dates[i], last_hedge, combo['hedge_recompute']):
            lookback_a = a_prices[max(0, i - 252): i]
            lookback_b = b_prices[max(0, i - 252): i]
            beta, _    = compute_hedge_ratio(lookback_a, lookback_b)
            last_hedge = dates[i]

        spread = price_a - beta * price_b
        spreads.append(spread)
        if len(spreads) < window:
            continue
        roll   = spreads[-window:]
        mean_s = float(np.mean(roll))
        std_s  = float(np.std(roll))
        if std_s == 0:
            continue
        z = (spread - mean_s) / std_s

        if i >= REGIME_WINDOW:
            recent_a    = a_prices[i - REGIME_WINDOW: i]
            recent_b    = b_prices[i - REGIME_WINDOW: i]
            regime_corr = float(np.corrcoef(recent_a, recent_b)[0, 1])
            if regime_corr < REGIME_CORR_MIN:
                if position is not None:
                    hold = (today - position['entry_date']).days
                    pnl_a, pnl_b, total_pnl, ret_pct = _calc_pnl(
                        position, price_a, price_b, lot_a, lot_b)
                    trades.append(_make_trade(
                        position, sym_a, sym_b, today, z,
                        price_a, price_b, lot_a, lot_b,
                        pnl_a, pnl_b, total_pnl, ret_pct, hold,
                        'REGIME_STOP', combo['id']))
                    position = None
                continue

        if position is not None:
            hold        = (today - position['entry_date']).days
            direction   = position['direction']
            exit_reason = None

            if direction == 'LONG_SPREAD'  and z <= -combo['z_stop']:
                exit_reason = 'Z_STOP'
            elif direction == 'SHORT_SPREAD' and z >= combo['z_stop']:
                exit_reason = 'Z_STOP'
            elif direction == 'LONG_SPREAD'  and z >= -combo['z_exit']:
                exit_reason = 'Z_EXIT'
            elif direction == 'SHORT_SPREAD' and z <= combo['z_exit']:
                exit_reason = 'Z_EXIT'
            elif hold >= time_stop:
                exit_reason = 'TIME_STOP'

            if exit_reason:
                pnl_a, pnl_b, total_pnl, ret_pct = _calc_pnl(
                    position, price_a, price_b, lot_a, lot_b)
                trades.append(_make_trade(
                    position, sym_a, sym_b, today, z,
                    price_a, price_b, lot_a, lot_b,
                    pnl_a, pnl_b, total_pnl, ret_pct, hold,
                    exit_reason, combo['id']))
                position = None

        if position is None:
            if z <= -combo['z_entry']:
                position = {
                    'direction':     'LONG_SPREAD',
                    'entry_date':    today,
                    'entry_price_a': price_a,
                    'entry_price_b': price_b,
                    'entry_z':       z,
                    'beta':          beta,
                }
            elif z >= combo['z_entry']:
                position = {
                    'direction':     'SHORT_SPREAD',
                    'entry_date':    today,
                    'entry_price_a': price_a,
                    'entry_price_b': price_b,
                    'entry_z':       z,
                    'beta':          beta,
                }

    if position is not None:
        today   = dates[-1].date()
        price_a = float(a_prices[-1])
        price_b = float(b_prices[-1])
        hold    = (today - position['entry_date']).days
        pnl_a, pnl_b, total_pnl, ret_pct = _calc_pnl(
            position, price_a, price_b, lot_a, lot_b)
        trades.append(_make_trade(
            position, sym_a, sym_b, today, 0.0,
            price_a, price_b, lot_a, lot_b,
            pnl_a, pnl_b, total_pnl, ret_pct, hold,
            'PERIOD_END', combo['id']))

    if not trades:
        return None, []

    pnls     = [t['total_pnl'] for t in trades]
    returns  = [t['return_pct'] for t in trades]
    holdings = [t['holding_days'] for t in trades]
    reasons  = [t['exit_reason'] for t in trades]
    total    = len(trades)
    wins     = sum(1 for p in pnls if p > 0)
    stops    = sum(1 for r in reasons if r == 'Z_STOP')
    tstops   = sum(1 for r in reasons if r == 'TIME_STOP')
    cum_pnl  = list(np.cumsum(pnls))

    metrics = {
        'combo_id':         int(combo['id']),
        'z_entry':          float(combo['z_entry']),
        'z_exit':           float(combo['z_exit']),
        'z_stop':           float(combo['z_stop']),
        'zscore_window':    int(combo['window']),
        'hedge_recompute':  combo['hedge_recompute'],
        'symbol_a':         sym_a,
        'symbol_b':         sym_b,
        'segment':          pair['segment'],
        'total_trades':     int(total),
        'win_trades':       int(wins),
        'loss_trades':      int(max(0, total - wins - stops - tstops)),
        'stop_trades':      int(stops),
        'time_stop_trades': int(tstops),
        'win_rate':         round(float(wins / total * 100), 2),
        'stop_rate':        round(float(stops / total * 100), 2),
        'time_stop_rate':   round(float(tstops / total * 100), 2),
        'total_pnl':        round(float(sum(pnls)), 2),
        'avg_return_pct':   round(float(np.mean(returns)), 4),
        'avg_holding_days': round(float(np.mean(holdings)), 1),
        'max_drawdown':     round(float(compute_max_drawdown(cum_pnl)), 4),
        'sharpe_ratio':     round(float(compute_sharpe(returns)), 4),
        'profit_factor':    round(float(compute_profit_factor(pnls)), 4),
        'worst_trade':      round(float(min(pnls)), 2),
        'max_dd_rupees':    round(float(compute_max_dd_rupees(cum_pnl)), 2),
        'backtest_period':  f"{BACKTEST_START} to {BACKTEST_END}",
    }
    return metrics, trades


def store_results(conn, metrics_list: List[dict], all_trades: List[dict]):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pair_backtest_results")
        cur.execute("DELETE FROM pair_backtest_trades")
        for m in metrics_list:
            cur.execute("""
                INSERT INTO pair_backtest_results
                (combo_id,z_entry,z_exit,z_stop,zscore_window,hedge_recompute,
                 symbol_a,symbol_b,segment,total_trades,win_trades,loss_trades,
                 stop_trades,time_stop_trades,win_rate,stop_rate,time_stop_rate,
                 total_pnl,avg_return_pct,avg_holding_days,max_drawdown,
                 sharpe_ratio,profit_factor,worst_trade,max_dd_rupees,backtest_period)
                VALUES (%(combo_id)s,%(z_entry)s,%(z_exit)s,%(z_stop)s,%(zscore_window)s,
                        %(hedge_recompute)s,%(symbol_a)s,%(symbol_b)s,%(segment)s,
                        %(total_trades)s,%(win_trades)s,%(loss_trades)s,%(stop_trades)s,
                        %(time_stop_trades)s,%(win_rate)s,%(stop_rate)s,%(time_stop_rate)s,
                        %(total_pnl)s,%(avg_return_pct)s,%(avg_holding_days)s,
                        %(max_drawdown)s,%(sharpe_ratio)s,%(profit_factor)s,
                        %(worst_trade)s,%(max_dd_rupees)s,%(backtest_period)s)
            """, m)
        for t in all_trades:
            cur.execute("""
                INSERT INTO pair_backtest_trades
                (combo_id,symbol_a,symbol_b,direction,entry_date,exit_date,
                 entry_z,exit_z,entry_price_a,entry_price_b,exit_price_a,
                 exit_price_b,lot_size_a,lot_size_b,pnl_a,pnl_b,total_pnl,
                 return_pct,holding_days,exit_reason)
                VALUES (%(combo_id)s,%(symbol_a)s,%(symbol_b)s,%(direction)s,
                        %(entry_date)s,%(exit_date)s,%(entry_z)s,%(exit_z)s,
                        %(entry_price_a)s,%(entry_price_b)s,%(exit_price_a)s,
                        %(exit_price_b)s,%(lot_size_a)s,%(lot_size_b)s,
                        %(pnl_a)s,%(pnl_b)s,%(total_pnl)s,%(return_pct)s,
                        %(holding_days)s,%(exit_reason)s)
            """, t)
    conn.commit()


def run_backtest() -> dict:
    conn = get_conn()
    ensure_schema(conn)

    pairs = load_valid_pairs(conn)
    if not pairs:
        conn.close()
        return {"error": "no_pairs — run /api/v9/discover first"}

    all_symbols = list(set(
        [p['symbol_a'] for p in pairs] + [p['symbol_b'] for p in pairs]
    ))
    prices    = load_prices(conn, all_symbols)
    lot_sizes = load_lot_sizes(conn, all_symbols)

    log.info(f"V9 Backtest | {len(pairs)} pairs × {len(COMBOS)} combos | {BACKTEST_START} to {BACKTEST_END}")

    all_metrics   = []
    all_trades    = []
    combo_summary = {}

    for combo in COMBOS:
        combo_pnls   = []
        combo_trades = 0
        combo_wins   = 0
        combo_holds  = []

        for pair in pairs:
            metrics, trades = backtest_pair(prices, lot_sizes, pair, combo)
            if metrics and metrics['total_trades'] > 0:
                all_metrics.append(metrics)
                all_trades.extend(trades)
                combo_pnls.append(metrics['total_pnl'])
                combo_trades += metrics['total_trades']
                combo_wins   += metrics['win_trades']
                combo_holds.extend([t['holding_days'] for t in trades])

        win_rate = round(combo_wins / combo_trades * 100, 1) if combo_trades else 0
        avg_hold = round(float(np.mean(combo_holds)), 1) if combo_holds else 0
        combo_summary[combo['id']] = {
            'total_pnl':    round(float(sum(combo_pnls)), 2),
            'total_trades': int(combo_trades),
            'win_rate':     win_rate,
            'avg_hold':     avg_hold,
            'pairs_active': len(combo_pnls),
        }
        log.info(f"Combo {combo['id']}: pairs={len(combo_pnls)} "
                 f"trades={combo_trades} win%={win_rate} hold={avg_hold}d "
                 f"pnl=₹{sum(combo_pnls):,.0f}")

    store_results(conn, all_metrics, all_trades)
    conn.close()

    best = max(combo_summary.items(), key=lambda x: x[1]['total_pnl'])
    return {
        "status":        "ok",
        "period":        f"{BACKTEST_START} to {BACKTEST_END}",
        "pairs_tested":  len(pairs),
        "combos":        len(COMBOS),
        "total_runs":    len(all_metrics),
        "combo_summary": combo_summary,
        "best_combo":    best[0],
    }


if __name__ == '__main__':
    print(run_backtest())
