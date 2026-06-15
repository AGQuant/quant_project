"""
V8 Intraday Backtester — buy_reversal filter optimizer (v1.0)
=============================================================
Data:
  intraday_prices  — 5-min OHLC, rolling 6-7 trading days
  v8_metrics       — daily EOD filter values per symbol
  v8_paper_pivots  — rolling-5d PP/R1/S1 per symbol

Simulation:
  Entry: first 5-min bar where pp < close <= r1
         AND (r1-close) >= 0.5*(r1-pp)
         AND filter score >= threshold
  Target: R1 (bar HIGH >= R1 → WIN, exit at R1)
  Stop:   S1 (bar LOW  <= S1 → LOSS, exit at S1)
  Max hold: 3 trading days. Still open after = OPEN (excluded from stats).

Filter sweep: one parameter at a time, all others at baseline.
Score threshold: n + offset  (-2 = allows 2 filters to fail = loose).
"""

import os, psycopg, pandas as pd, numpy as np
from datetime import datetime, timedelta, timezone
from collections import defaultdict

DATABASE_URL = os.getenv("DATABASE_URL", "")

IST = timezone(timedelta(hours=5, minutes=30))

BASELINE = {
    "gvm_score":    (6.0,  10.0),
    "dma_200":      (1.5,  20.0),
    "dma_50":       (1.5,  8.0),
    "month_return": (0.0,  7.2),
    "week_return":  (1.0,  4.0),
    "rsi_month":    (45.0, 80.0),
    "rsi_weekly":   (50.0, 67.5),
    "mom_2d":       (0.0,  2.4),
    "sector_week":  (0.0,  6.0),
    "sector_month": (0.0,  6.0),
}

SWEEP = {
    "rsi_month":    [(40,80),(45,80),(50,80),(55,80),(45,75),(50,75),(50,85)],
    "rsi_weekly":   [(45,70),(50,67.5),(50,72),(55,72),(55,70),(45,67),(60,75)],
    "mom_2d":       [(0,2.0),(0,2.4),(0,3.0),(0,1.5),(0.5,2.4),(-0.5,2.4)],
    "week_return":  [(0.5,4),(1,4),(1.5,4),(1,5),(0,4),(0.5,5)],
    "month_return": [(0,5),(0,7.2),(0,10),(2,8),(1,7.2),(0,15)],
    "sector_month": [(-2,6),(0,6),(-5,6),(-5,10),(-1,6),(None,6)],
    "sector_week":  [(-2,6),(0,6),(-1,6),(0,8),(None,6),(-2,8)],
    "dma_50":       [(1,8),(1.5,8),(2,8),(0.5,8),(1,10),(1,6)],
    "dma_200":      [(1,15),(1.5,20),(2,20),(1,25),(0.5,15),(2,15)],
    "gvm_score":    [(5.5,10),(6,10),(6.5,10),(7,10),(5,10)],
}

METRIC_COLS = list(BASELINE.keys())


# ── helpers ──────────────────────────────────────────────────────────────────

def _passes(val, mn, mx):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return False
    if mn is not None and val < mn: return False
    if mx is not None and val > mx: return False
    return True

def _score(row, config):
    return sum(1 for m, (mn, mx) in config.items()
               if _passes(row.get(m), mn, mx))

def _pivot_room_ok(close, pp, r1):
    if close is None or pp is None or r1 is None: return False
    band = r1 - pp
    return band > 0 and pp < close <= r1 and (r1 - close) >= 0.5 * band


# ── data loading ─────────────────────────────────────────────────────────────

def load_all(conn):
    cur = conn.cursor()

    cur.execute("""
        SELECT i.symbol, i.ts, i.open, i.high, i.low, i.close
        FROM intraday_prices i
        WHERE i.timeframe = '5m'
          AND i.ts::date >= (SELECT MIN(ts::date) FROM intraday_prices WHERE timeframe='5m')
          AND i.close IS NOT NULL
        ORDER BY i.symbol, i.ts
    """)
    bars = pd.DataFrame(cur.fetchall(),
        columns=['symbol','ts','open','high','low','close'])
    bars['ts']   = pd.to_datetime(bars['ts'])
    bars['date'] = bars['ts'].dt.date

    cur.execute("""
        SELECT DISTINCT ON (symbol, score_date)
            symbol, score_date,
            gvm_score, dma_200, dma_50, month_return, week_return,
            rsi_month, rsi_weekly, mom_2d, sector_week, sector_month,
            day_1d, daily_rsi
        FROM v8_metrics
        ORDER BY symbol, score_date DESC
    """)
    met = pd.DataFrame(cur.fetchall(), columns=[
        'symbol','score_date','gvm_score','dma_200','dma_50',
        'month_return','week_return','rsi_month','rsi_weekly',
        'mom_2d','sector_week','sector_month','day_1d','daily_rsi'
    ])
    met['score_date'] = pd.to_datetime(met['score_date']).dt.date
    for c in METRIC_COLS + ['day_1d','daily_rsi']:
        met[c] = pd.to_numeric(met[c], errors='coerce')

    cur.execute("""
        SELECT DISTINCT ON (symbol)
            symbol, pivot_date, pp, r1, s1
        FROM v8_paper_pivots
        WHERE pp IS NOT NULL AND r1 IS NOT NULL AND s1 IS NOT NULL
        ORDER BY symbol, pivot_date DESC
    """)
    pivots = {r[0]: {'pp': float(r[2]), 'r1': float(r[3]), 's1': float(r[4])}
              for r in cur.fetchall()}

    cur.close()
    return bars, met, pivots


# ── simulation core ──────────────────────────────────────────────────────────

def simulate_basket(bars: pd.DataFrame, met: pd.DataFrame,
                    pivots: dict, config: dict,
                    score_thresh_offset: int = -2) -> list:
    n    = len(config)
    need = max(n + score_thresh_offset, 1)

    met_idx = {}
    for _, row in met.iterrows():
        met_idx[(row['symbol'], row['score_date'])] = row.to_dict()

    all_bars = bars.sort_values(['symbol','ts'])
    syms     = all_bars['symbol'].unique()
    trades   = []

    ENTRY_CUTOFF = 15 * 60 + 20   # 15:20 IST
    MARKET_CLOSE = 15 * 60 + 30   # 15:30 IST

    for sym in syms:
        sb = all_bars[all_bars['symbol'] == sym].reset_index(drop=True)
        pv = pivots.get(sym)
        if not pv:
            continue
        pp, r1, s1 = pv['pp'], pv['r1'], pv['s1']

        traded_dates = set()
        in_trade     = False
        trade_entry  = None

        for i in range(len(sb)):
            bar  = sb.iloc[i]
            ts   = bar['ts']
            d    = ts.date()
            mins = ts.hour * 60 + ts.minute

            if mins < 9 * 60 + 15 or mins > MARKET_CLOSE:
                continue

            if in_trade:
                hi, lo = float(bar['high']), float(bar['low'])
                if hi >= r1:
                    pnl = (r1 - trade_entry['price']) / trade_entry['price'] * 100
                    trades.append({**trade_entry, 'exit': r1, 'result': 'WIN',
                                   'pnl_pct': round(pnl, 3), 'exit_ts': ts})
                    in_trade = False
                elif lo <= s1:
                    pnl = (s1 - trade_entry['price']) / trade_entry['price'] * 100
                    trades.append({**trade_entry, 'exit': s1, 'result': 'LOSS',
                                   'pnl_pct': round(pnl, 3), 'exit_ts': ts})
                    in_trade = False
                elif mins >= MARKET_CLOSE:
                    days_held = (d - trade_entry['date']).days
                    if days_held >= 3:
                        pnl = (float(bar['close']) - trade_entry['price']) / trade_entry['price'] * 100
                        trades.append({**trade_entry, 'exit': float(bar['close']),
                                       'result': 'OPEN', 'pnl_pct': round(pnl, 3),
                                       'exit_ts': ts})
                        in_trade = False
                continue

            if d in traded_dates or mins > ENTRY_CUTOFF:
                continue

            m_key   = (sym, d)
            met_row = met_idx.get(m_key)
            if met_row is None:
                met_row = met_idx.get((sym, d - timedelta(days=1)))
            if met_row is None:
                continue

            if _score(met_row, config) < need:
                continue

            close = float(bar['close'])
            if _pivot_room_ok(close, pp, r1):
                r_reward = r1 - close
                r_risk   = close - s1
                rr = round(r_reward / r_risk, 2) if r_risk > 0 else None
                trade_entry = {
                    'symbol': sym, 'date': d, 'entry_ts': ts,
                    'price': close, 'r1': r1, 's1': s1, 'pp': pp,
                    'rr': rr, 'filter_score': _score(met_row, config),
                    'filter_total': n,
                }
                in_trade = True
                traded_dates.add(d)

        if in_trade and trade_entry:
            last = sb.iloc[-1]
            pnl  = (float(last['close']) - trade_entry['price']) / trade_entry['price'] * 100
            trades.append({**trade_entry, 'exit': float(last['close']),
                           'result': 'OPEN', 'pnl_pct': round(pnl, 3),
                           'exit_ts': last['ts']})

    return trades


def stats(trades: list, label: str = "") -> dict:
    closed = [t for t in trades if t['result'] in ('WIN', 'LOSS')]
    if not closed:
        return {'label': label, 'n': 0, 'win_rate': None, 'avg_pnl': None,
                'avg_win': None, 'avg_loss': None, 'score': 0}
    wins   = [t for t in closed if t['result'] == 'WIN']
    losses = [t for t in closed if t['result'] == 'LOSS']
    wr     = len(wins) / len(closed)
    avg_p  = float(np.mean([t['pnl_pct'] for t in closed]))
    avg_w  = float(np.mean([t['pnl_pct'] for t in wins]))   if wins   else None
    avg_l  = float(np.mean([t['pnl_pct'] for t in losses])) if losses else None
    score  = avg_p * wr * np.sqrt(len(closed))
    return {
        'label': label,
        'n': len(closed), 'wins': len(wins), 'losses': len(losses),
        'win_rate':  round(wr, 3),
        'avg_pnl':   round(avg_p, 3),
        'avg_win':   round(avg_w, 3) if avg_w is not None else None,
        'avg_loss':  round(avg_l, 3) if avg_l is not None else None,
        'score':     round(float(score), 4),
    }


# ── per-filter sweep ──────────────────────────────────────────────────────────

def sweep_one_filter(param: str, bars, met, pivots,
                     score_offset: int = -2) -> list:
    results = []
    for (mn, mx) in SWEEP.get(param, []):
        cfg = dict(BASELINE)
        cfg[param] = (mn, mx)
        trades = simulate_basket(bars, met, pivots, cfg, score_offset)
        s = stats(trades, label=f"[{mn},{mx}]")
        s.update({'param': param, 'min': mn, 'max': mx})
        results.append(s)

    # Baseline value
    cfg    = dict(BASELINE)
    trades = simulate_basket(bars, met, pivots, cfg, score_offset)
    s = stats(trades, label=f"baseline {BASELINE[param]}")
    s.update({'param': param, 'min': BASELINE[param][0],
              'max': BASELINE[param][1], 'is_baseline': True})
    results.append(s)
    results.sort(key=lambda x: (x['score'] or -999), reverse=True)
    return results


# ── main optimizer ────────────────────────────────────────────────────────────

def run_optimizer(basket: str = 'buy_reversal',
                  score_offset: int = -2) -> dict:
    with psycopg.connect(DATABASE_URL) as conn:
        bars, met, pivots = load_all(conn)

    baseline_trades = simulate_basket(bars, met, pivots, BASELINE, score_offset)
    baseline_stats  = stats(baseline_trades, "BASELINE")

    all_results = {}
    for param in SWEEP:
        results = sweep_one_filter(param, bars, met, pivots, score_offset)
        all_results[param] = results

    # Build recommended config from best-scoring value per parameter
    recommended = {}
    summary     = []
    for param, results in all_results.items():
        best = results[0]
        base = next((r for r in results if r.get('is_baseline')), None)
        recommended[param] = (best['min'], best['max'])
        changed = (best['min'] != BASELINE[param][0] or
                   best['max'] != BASELINE[param][1])
        summary.append({
            'param':         param,
            'current':       list(BASELINE[param]),
            'recommended':   [best['min'], best['max']],
            'changed':       changed,
            'win_rate_best': best['win_rate'],
            'win_rate_base': base['win_rate'] if base else None,
            'n_best':        best['n'],
            'avg_pnl_best':  best['avg_pnl'],
            'score_best':    best['score'],
        })

    rec_trades = simulate_basket(bars, met, pivots, recommended, score_offset)
    rec_stats  = stats(rec_trades, "RECOMMENDED")

    return {
        'basket':             basket,
        'score_offset':       score_offset,
        'intraday_days':      int(bars['date'].nunique()),
        'symbols':            int(bars['symbol'].nunique()),
        'baseline':           baseline_stats,
        'recommended':        rec_stats,
        'recommended_config': {k: list(v) for k, v in recommended.items()},
        'summary':            summary,
        'per_param':          all_results,
    }
