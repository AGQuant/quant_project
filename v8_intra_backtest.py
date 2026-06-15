"""
V8 Intraday Backtester — buy_reversal filter optimizer (v2.0)
=============================================================
v2.0 changes (15-Jun-2026):
  - Futures-only: filters to futures_universe WHERE is_active=TRUE
  - No re-entry while open: in_trade blocks symbol across ALL days
    until position closes (WIN / LOSS / max-hold OPEN)
  - Writes to DB: v8_backtest_log — entry row on open, updated on exit
  - Market hours: 09:15 entry open, 15:20 entry cutoff, 15:30 exit close
  - ts in intraday_prices stored as IST — no +5:30 conversion needed

Data:
  intraday_prices  — 5-min OHLC, rolling 6-7 trading days (IST timestamps)
  v8_metrics       — daily EOD filter values per symbol
  v8_paper_pivots  — rolling-5d PP/R1/S1 per symbol
  futures_universe — active futures symbols only

Simulation logic per symbol:
  1. Filter by EOD score >= threshold (score-based, not strict all-pass)
  2. Scan bars 09:15-15:20 IST for pivot-room entry:
       pp < close <= r1  AND  (r1-close) >= 50% * (r1-pp)
  3. While in_trade: scan bars to 15:30 for:
       HIGH >= R1  → WIN at R1
       LOW  <= S1  → LOSS at S1
     At 15:30 EOD: carry to next day (no forced exit at close)
     Max hold = 3 trading days → OPEN (force-close at last bar price)
  4. No new entry on same symbol until current position closes
"""

import os, uuid, psycopg, pandas as pd, numpy as np
from datetime import datetime, timedelta, timezone, date as dt_date

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── market hours (minutes since midnight, IST) ────────────────────────────
MKT_OPEN  = 9  * 60 + 15   # 09:15
ENTRY_CUT = 15 * 60 + 20   # 15:20  (no new entries after this)
MKT_CLOSE = 15 * 60 + 30   # 15:30  (exit tracking stops here each day)
MAX_HOLD_DAYS = 3

# ── baseline filter config ────────────────────────────────────────────────
BASELINE = {
    "gvm_score":    (6.0,  10.0),
    "dma_200":      (1.5,  20.0),
    "dma_50":       (1.5,   8.0),
    "month_return": (0.0,   7.2),
    "week_return":  (1.0,   4.0),
    "rsi_month":    (45.0, 80.0),
    "rsi_weekly":   (50.0, 67.5),
    "mom_2d":       (0.0,   2.4),
    "sector_week":  (0.0,   6.0),
    "sector_month": (0.0,   6.0),
}

SWEEP = {
    "rsi_month":    [(40,80),(45,80),(50,80),(55,80),(45,75),(50,75),(50,85)],
    "rsi_weekly":   [(45,70),(50,67.5),(50,72),(55,72),(55,70),(45,67),(60,75)],
    "mom_2d":       [(0,2.0),(0,2.4),(0,3.0),(0,1.5),(0.5,2.4),(-0.5,2.4)],
    "week_return":  [(0.5,4),(1,4),(1.5,4),(1,5),(0,4),(0.5,5),(-1,4)],
    "month_return": [(0,5),(0,7.2),(0,10),(2,8),(1,7.2),(0,15)],
    "sector_month": [(-2,6),(0,6),(-5,6),(-5,10),(-1,6),(None,6)],
    "sector_week":  [(-2,6),(0,6),(-1,6),(0,8),(None,6),(-2,8)],
    "dma_50":       [(1,8),(1.5,8),(2,8),(0.5,8),(1,10),(1,6)],
    "dma_200":      [(1,15),(1.5,20),(2,20),(1,25),(0.5,15),(2,15)],
    "gvm_score":    [(5.5,10),(6,10),(6.5,10),(7,10),(5,10)],
}

METRIC_COLS = list(BASELINE.keys())


# ── DB table ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v8_backtest_log (
    id           SERIAL PRIMARY KEY,
    run_id       TEXT        NOT NULL,
    basket       TEXT        NOT NULL,
    symbol       TEXT        NOT NULL,
    entry_date   DATE,
    entry_ts     TIMESTAMP,
    entry_price  NUMERIC,
    pp           NUMERIC,
    r1           NUMERIC,
    s1           NUMERIC,
    rr           NUMERIC,
    filter_score INTEGER,
    exit_ts      TIMESTAMP,
    exit_price   NUMERIC,
    result       TEXT,
    pnl_pct      NUMERIC,
    created_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_v8bt_run  ON v8_backtest_log(run_id);
CREATE INDEX IF NOT EXISTS idx_v8bt_sym  ON v8_backtest_log(symbol, entry_date);
"""


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


# ── helpers ───────────────────────────────────────────────────────────────

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
    """Entry condition: between PP and R1, with >= 50% room to R1."""
    if close is None or pp is None or r1 is None:
        return False
    band = r1 - pp
    return band > 0 and pp < close <= r1 and (r1 - close) >= 0.5 * band


def _mins(ts) -> int:
    """Minutes since midnight from a pandas Timestamp (IST stored directly)."""
    return ts.hour * 60 + ts.minute


# ── data loading ──────────────────────────────────────────────────────────

def load_all(conn):
    """
    Load intraday bars, EOD metrics, and pivots.
    Filters to futures_universe only.
    ts in intraday_prices is stored as IST — no conversion needed.
    """
    cur = conn.cursor()

    # Futures universe
    cur.execute("""
        SELECT symbol FROM futures_universe WHERE is_active = TRUE
    """)
    futures_syms = {r[0] for r in cur.fetchall()}

    # Intraday bars — futures only, IST timestamps
    cur.execute("""
        SELECT i.symbol, i.ts, i.open, i.high, i.low, i.close
        FROM intraday_prices i
        JOIN futures_universe f ON f.symbol = i.symbol AND f.is_active = TRUE
        WHERE i.timeframe = '5m'
          AND i.ts::date >= (
              SELECT MIN(ts::date) FROM intraday_prices WHERE timeframe='5m'
          )
          AND i.close IS NOT NULL
        ORDER BY i.symbol, i.ts
    """)
    bars = pd.DataFrame(cur.fetchall(),
        columns=['symbol','ts','open','high','low','close'])
    bars['ts']   = pd.to_datetime(bars['ts'])
    bars['date'] = bars['ts'].dt.date

    # EOD metrics
    cur.execute("""
        SELECT DISTINCT ON (symbol, score_date)
            symbol, score_date,
            gvm_score, dma_200, dma_50, month_return, week_return,
            rsi_month, rsi_weekly, mom_2d, sector_week, sector_month,
            day_1d, daily_rsi
        FROM v8_metrics
        WHERE symbol IN (
            SELECT symbol FROM futures_universe WHERE is_active=TRUE
        )
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

    # Pivots — latest per symbol
    cur.execute("""
        SELECT DISTINCT ON (symbol)
            symbol, pivot_date, pp, r1, s1
        FROM v8_paper_pivots
        WHERE pp IS NOT NULL AND r1 IS NOT NULL AND s1 IS NOT NULL
          AND symbol IN (
              SELECT symbol FROM futures_universe WHERE is_active=TRUE
          )
        ORDER BY symbol, pivot_date DESC
    """)
    pivots = {r[0]: {'pp': float(r[2]), 'r1': float(r[3]), 's1': float(r[4])}
              for r in cur.fetchall()}

    cur.close()
    print(f"  Loaded: {len(bars):,} bars | "
          f"{bars['symbol'].nunique()} futures | "
          f"{bars['date'].nunique()} days | "
          f"{len(pivots)} pivots")
    return bars, met, pivots


# ── simulation core ───────────────────────────────────────────────────────

def simulate_basket(bars: pd.DataFrame,
                    met: pd.DataFrame,
                    pivots: dict,
                    config: dict,
                    score_thresh_offset: int = -2,
                    write_db: bool = False,
                    conn=None,
                    basket: str = 'buy_reversal',
                    run_id: str = None) -> list:
    """
    Simulate buy_reversal strategy.

    Entry rules:
      - score >= (n + offset)
      - 09:15 <= bar_time <= 15:20
      - pp < close <= r1  AND  room >= 50%
      - NOT already in a trade on this symbol

    Exit rules:
      - HIGH >= R1  → WIN
      - LOW  <= S1  → LOSS
      - 15:30 EOD   → hold (carry to next day, no forced daily exit)
      - MAX_HOLD_DAYS exceeded at 15:30 → OPEN (force-close)

    No re-entry while position is open (in_trade guards across days).
    """
    n    = len(config)
    need = max(n + score_thresh_offset, 1)

    if write_db and conn and not run_id:
        run_id = f"{basket}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    # Build metric lookup: (symbol, date) → row dict
    met_idx = {}
    for _, row in met.iterrows():
        met_idx[(row['symbol'], row['score_date'])] = row.to_dict()

    all_bars = bars.sort_values(['symbol', 'ts'])
    syms     = all_bars['symbol'].unique()
    trades   = []

    for sym in syms:
        sb = all_bars[all_bars['symbol'] == sym].reset_index(drop=True)
        pv = pivots.get(sym)
        if not pv:
            continue
        pp, r1, s1 = pv['pp'], pv['r1'], pv['s1']

        # State machine
        in_trade   = False
        trade_entry = None
        db_row_id   = None   # ID of the v8_backtest_log row for this trade

        for i in range(len(sb)):
            bar  = sb.iloc[i]
            ts   = bar['ts']
            d    = ts.date()
            mins = _mins(ts)

            # Skip pre/post market entirely
            if mins < MKT_OPEN or mins > MKT_CLOSE:
                continue

            # ── EXIT logic (while in_trade) ───────────────────────────────
            if in_trade:
                hi, lo = float(bar['high']), float(bar['low'])

                result = exit_price = exit_ts = None

                if hi >= r1:
                    result, exit_price, exit_ts = 'WIN', r1, ts
                elif lo <= s1:
                    result, exit_price, exit_ts = 'LOSS', s1, ts
                elif mins >= MKT_CLOSE:
                    days_held = (d - trade_entry['date']).days
                    if days_held >= MAX_HOLD_DAYS:
                        result = 'OPEN'
                        exit_price = float(bar['close'])
                        exit_ts    = ts

                if result:
                    pnl = (exit_price - trade_entry['price']) / trade_entry['price'] * 100
                    trade = {
                        **trade_entry,
                        'exit': exit_price,
                        'result': result,
                        'pnl_pct': round(pnl, 3),
                        'exit_ts': exit_ts,
                    }
                    trades.append(trade)
                    in_trade = False

                    # Write exit to DB
                    if write_db and conn and db_row_id:
                        _db_update_exit(conn, db_row_id, exit_ts, exit_price,
                                        result, round(pnl, 3))
                continue

            # ── ENTRY logic (not in trade) ────────────────────────────────
            if mins > ENTRY_CUT:
                continue   # past entry cutoff

            # Get latest EOD metrics for this symbol on this date
            met_row = met_idx.get((sym, d))
            if met_row is None:
                # fallback: previous trading day
                met_row = met_idx.get((sym, d - timedelta(days=1)))
            if met_row is None:
                continue

            # Filter score gate
            if _score(met_row, config) < need:
                continue

            # Pivot-room entry condition
            close = float(bar['close'])
            if not _pivot_room_ok(close, pp, r1):
                continue

            # ── ENTER ────────────────────────────────────────────────────
            r_reward = r1 - close
            r_risk   = close - s1
            rr       = round(r_reward / r_risk, 2) if r_risk > 0 else None
            sc       = _score(met_row, config)

            trade_entry = {
                'symbol':       sym,
                'date':         d,
                'entry_ts':     ts,
                'price':        close,
                'r1':           r1,
                's1':           s1,
                'pp':           pp,
                'rr':           rr,
                'filter_score': sc,
                'filter_total': n,
            }
            in_trade  = True
            db_row_id = None

            # Write entry to DB
            if write_db and conn:
                db_row_id = _db_insert_entry(conn, run_id, basket, trade_entry)

        # ── end of data: flush open trade ─────────────────────────────────
        if in_trade and trade_entry:
            last       = sb.iloc[-1]
            exit_price = float(last['close'])
            exit_ts    = last['ts']
            pnl        = (exit_price - trade_entry['price']) / trade_entry['price'] * 100
            trade      = {
                **trade_entry,
                'exit':    exit_price,
                'result':  'OPEN',
                'pnl_pct': round(pnl, 3),
                'exit_ts': exit_ts,
            }
            trades.append(trade)
            if write_db and conn and db_row_id:
                _db_update_exit(conn, db_row_id, exit_ts, exit_price,
                                'OPEN', round(pnl, 3))

    return trades


# ── DB I/O ────────────────────────────────────────────────────────────────

def _db_insert_entry(conn, run_id: str, basket: str, te: dict) -> int:
    """Insert an open entry row; returns the new row id."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_backtest_log
                  (run_id, basket, symbol, entry_date, entry_ts,
                   entry_price, pp, r1, s1, rr, filter_score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                run_id, basket, te['symbol'], te['date'],
                te['entry_ts'], te['price'],
                te['pp'], te['r1'], te['s1'],
                te['rr'], te['filter_score'],
            ))
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    except Exception as e:
        print(f"  [DB] insert entry failed for {te['symbol']}: {e}")
        try: conn.rollback()
        except: pass
        return None


def _db_update_exit(conn, row_id: int, exit_ts, exit_price: float,
                    result: str, pnl_pct: float):
    """Update an existing entry row with exit data."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE v8_backtest_log
                   SET exit_ts    = %s,
                       exit_price = %s,
                       result     = %s,
                       pnl_pct    = %s
                 WHERE id = %s
            """, (exit_ts, exit_price, result, pnl_pct, row_id))
        conn.commit()
    except Exception as e:
        print(f"  [DB] update exit failed (id={row_id}): {e}")
        try: conn.rollback()
        except: pass


# ── stats helper ──────────────────────────────────────────────────────────

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
        'label':    label,
        'n':        len(closed),
        'wins':     len(wins),
        'losses':   len(losses),
        'open':     len([t for t in trades if t['result'] == 'OPEN']),
        'win_rate': round(wr, 3),
        'avg_pnl':  round(avg_p, 3),
        'avg_win':  round(avg_w, 3)  if avg_w  is not None else None,
        'avg_loss': round(avg_l, 3)  if avg_l  is not None else None,
        'score':    round(float(score), 4),
    }


# ── per-filter sweep (optimizer — no DB writes) ───────────────────────────

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

    # Baseline for comparison
    cfg    = dict(BASELINE)
    trades = simulate_basket(bars, met, pivots, cfg, score_offset)
    s = stats(trades, label=f"baseline {BASELINE[param]}")
    s.update({'param': param, 'min': BASELINE[param][0],
              'max': BASELINE[param][1], 'is_baseline': True})
    results.append(s)
    results.sort(key=lambda x: (x['score'] or -999), reverse=True)
    return results


# ── main optimizer ────────────────────────────────────────────────────────

def run_optimizer(basket: str = 'buy_reversal',
                  score_offset: int = -2,
                  write_db: bool = False) -> dict:
    with psycopg.connect(DATABASE_URL) as conn:
        ensure_table(conn)
        bars, met, pivots = load_all(conn)

    baseline_trades = simulate_basket(bars, met, pivots, BASELINE, score_offset)
    baseline_stats  = stats(baseline_trades, "BASELINE")
    print(f"Baseline: {baseline_stats}")

    all_results = {}
    for param in SWEEP:
        print(f"Sweeping {param}...")
        results = sweep_one_filter(param, bars, met, pivots, score_offset)
        all_results[param] = results
        best = results[0]
        print(f"  best [{best['min']},{best['max']}] "
              f"win={best['win_rate']} n={best['n']} score={best['score']}")

    # Build recommended config
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

    # Final run with recommended config — optionally write to DB
    run_id = f"{basket}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    with psycopg.connect(DATABASE_URL) as conn:
        rec_trades = simulate_basket(
            bars, met, pivots, recommended, score_offset,
            write_db=write_db, conn=conn if write_db else None,
            basket=basket, run_id=run_id
        )
    rec_stats = stats(rec_trades, "RECOMMENDED")
    print(f"Recommended: {rec_stats}")

    return {
        'basket':             basket,
        'run_id':             run_id,
        'score_offset':       score_offset,
        'intraday_days':      int(bars['date'].nunique()),
        'symbols':            int(bars['symbol'].nunique()),
        'baseline':           baseline_stats,
        'recommended':        rec_stats,
        'recommended_config': {k: list(v) for k, v in recommended.items()},
        'summary':            summary,
        'per_param':          {p: v[:5] for p, v in all_results.items()},
    }


def run_simulation(basket: str = 'buy_reversal',
                   score_offset: int = -2,
                   config: dict = None,
                   write_db: bool = True) -> dict:
    """
    Run a single simulation with given config and optionally write to DB.
    If config is None, uses BASELINE.
    """
    cfg    = config or BASELINE
    run_id = f"{basket}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    with psycopg.connect(DATABASE_URL) as conn:
        ensure_table(conn)
        bars, met, pivots = load_all(conn)
        trades = simulate_basket(
            bars, met, pivots, cfg, score_offset,
            write_db=write_db, conn=conn if write_db else None,
            basket=basket, run_id=run_id,
        )

    s = stats(trades, label=run_id)
    print(f"Simulation {run_id}: {s}")
    return {
        'run_id':    run_id,
        'basket':    basket,
        'config':    {k: list(v) for k, v in cfg.items()},
        'stats':     s,
        'trades':    [
            {k: (str(v) if isinstance(v, (dt_date, datetime)) else v)
             for k, v in t.items()}
            for t in trades
        ],
    }
