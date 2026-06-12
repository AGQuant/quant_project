"""
v8_paper.py — V8 Paper-Trading Engine (Scorr)
=============================================
Sprint-1, locked spec (Railway session_log id 37, 31-May-2026).
ISOLATED from real trades / personal_journal — own v8_paper_* tables.

FUNNEL (3 layers):
  1. Basket FILTERS first  — a stock is ELIGIBLE only if it passes a V8 basket's
     bands on the latest v8_metrics (EOD). Qualified set is fixed for the day.
     CANONICAL filters imported from v8_endpoints.FILTER_CONFIG (single source of truth).
  2. Rolling-5-day PIVOTS  — PP/R1/S1 from last 5 trading days (T-1..T-5) of
     raw_prices, recomputed nightly; window rolls daily.
  3. Zone trigger — on 5-min candle close, on qualified names only.

ENTRY (qualified name + zone condition + free slot + not blackout + before 15:20):
  BUY  : (r1-this_close) >= GAP_ROOM_FRAC*(r1-pp)
         -> enter @ close, target R1, SL = entry-(R1-entry)
  SHORT: (this_close-s1) >= GAP_ROOM_FRAC*(pp-s1)
         -> enter @ close, target S1, SL = entry+(entry-S1)
  + _traded_today guard: one entry per symbol/side/day maximum.
  No pp<close<=r1 band condition — only room-to-target fraction gate.
  GAP_ROOM_FRAC (0.3) = minimum remaining gap to target as a fraction of the
  pp->r1 (or pp->s1) band. Lower = looser entry, more signals, weaker R:R on
  marginal trades. Was 0.5; lowered to 0.3 on 11-Jun-2026 (founder decision).
  Band condition (pp < close <= r1) REMOVED 12-Jun-2026 (founder decision) —
  only the room fraction matters.

SELL_OVERBOUGHT (added 12-Jun-2026, founder decision):
  Separate basket with its OWN entry model — NOT the PP/R1/S1 zone trigger.
  Signal SQL (lifted from v8_endpoints.sell_overbought) filters on
  dma_200>=10, week_index_52>=80, ma9_vs_ma21>=3, vol_ratio<=0.8, mom_2d<0,
  rsi_month>=60, with target = prev-day S1 (a fixed pivot level).
  Paper entry rule (Option A, founder-locked 12-Jun-2026):
    - enter SHORT at the CURRENT 5-min close (enter whenever tick runs, no
      price sanity gate — "enter regardless"),
    - target = S1 (fixed pivot level from the signal),
    - stop = entry + (entry - S1)  -> recomputed off the LIVE entry to keep 1:1,
  Gated by slots / blackout / _traded_today / cutoff exactly like the others.
  Exits flow through the SAME generic exit block (target/SL/gap/gate).
  Signals are CACHED per-day (stable off EOD metrics) — see _SO_CACHE below.

EXIT (close-based, multi-day, levels frozen at entry):
  target hit / SL hit -> TARGET / SL
  gap at first bar of day past level -> GAP_TARGET_EXIT / GAP_SL_EXIT (exit at open)
  gate rebalance once at 15:20 both sides: open > current slots -> close excess
     order best-profit, worst-loss, 2nd-best, 2nd-worst... stop when enough -> GATE_EXIT

SIZING : 1 lot (futures_universe.lot_size, default 1).
GATE   : market-mood buy_slots/sell_slots (sum 15) cap concurrent open per side.
MISSED : qualified + zone trigger but blocked by slot_full|blackout|after_cutoff ->
         v8_paper_missed, one row per (symbol, side, day).

Price feed: intraday_prices (Fyers 5-min, NAIVE IST ts — read RAW, no TZ math).

FILTER_CONFIG: imported from v8_endpoints (canonical). Do NOT duplicate here.

PIVOT SELF-HEALING (added 12-Jun-2026, founder decision):
  paper_tick auto-computes today's pivots if absent before reading them, so the
  engine never trades on stale (prior-day) pivots while waiting for the 22:05
  nightly job. The nightly job (scheduler _task_build_paper_pivots) is RETAINED
  as belt-and-suspenders; this guard only fills a same-day gap. If a same-day
  build fails and the engine falls back to older pivots, a PIVOT DRIFT warning
  is logged (date-drift observability, 12-Jun-2026 follow-up).
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional, Dict, List

log = logging.getLogger("scorr.v8paper")

PIVOT_WINDOW   = 5
PIVOT_MIN_DAYS = 3
ENTRY_CUTOFF   = time(15, 20)
REBALANCE_TIME = time(15, 20)

# Minimum remaining gap to target as a fraction of the pp->r1 (or pp->s1) band.
# Entry requires the close to leave at least this much room to the target.
# 0.5 = close must be in lower/upper half of zone (room >= distance travelled, R:R >= 1:1).
# 0.3 = looser; close may travel up to 70% toward target. More signals, weaker R:R
#       on marginal trades. Lowered 0.5 -> 0.3 on 11-Jun-2026 (founder decision).
# Band condition (pp < close <= r1) REMOVED 12-Jun-2026 — only room fraction applies.
GAP_ROOM_FRAC  = 0.3

# ── sell_overbought signal cache (12-Jun-2026, follow-up) ────────────────────
# The signal set is built off EOD v8_metrics + prior-day pivots — nothing intraday
# changes it, so it is stable for the whole trading day. Caching it avoids running
# the 60-day-window SQL on every 1-min tick (~375 runs/day -> 1). Keyed by date.
# A manual force-refresh busts the cache when the EOD engine is rerun intraday
# (testing/backfills) so paper_tick doesn't serve stale signals until midnight.
_SO_CACHE: Dict = {"date": None, "signals": None}

def invalidate_sell_overbought_cache():
    """Bust the sell_overbought signal cache. Call after an intraday EOD-engine
    rerun so the next paper_tick recomputes signals instead of serving stale."""
    _SO_CACHE["date"] = None
    _SO_CACHE["signals"] = None
    log.info("sell_overbought signal cache invalidated")


# ============================================================ SCHEMA
PAPER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v8_paper_pivots (
    symbol TEXT NOT NULL, pivot_date DATE NOT NULL,
    window_start DATE, window_end DATE,
    pp NUMERIC, r1 NUMERIC, s1 NUMERIC, r2 NUMERIC, s2 NUMERIC,
    base_high NUMERIC, base_low NUMERIC, base_close NUMERIC, base_days INTEGER,
    built_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (symbol, pivot_date)
);
CREATE INDEX IF NOT EXISTS idx_paper_pivots_date ON v8_paper_pivots(pivot_date DESC);

CREATE TABLE IF NOT EXISTS v8_paper_positions (
    id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
    basket TEXT, entry_price NUMERIC NOT NULL, entry_ts TIMESTAMP NOT NULL,
    qty INTEGER NOT NULL, target NUMERIC NOT NULL, stop_loss NUMERIC NOT NULL,
    pp NUMERIC, pivot_date DATE, status TEXT DEFAULT 'OPEN',
    UNIQUE (symbol, side, status)
);
CREATE INDEX IF NOT EXISTS idx_paper_pos_status ON v8_paper_positions(status);

CREATE TABLE IF NOT EXISTS v8_paper_trades (
    id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL, basket TEXT,
    entry_price NUMERIC NOT NULL, entry_ts TIMESTAMP NOT NULL,
    exit_price NUMERIC NOT NULL, exit_ts TIMESTAMP NOT NULL, qty INTEGER NOT NULL,
    target NUMERIC, stop_loss NUMERIC, pnl NUMERIC, return_pct NUMERIC,
    result TEXT, pivot_date DATE, closed_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_sym ON v8_paper_trades(symbol, closed_at DESC);

CREATE TABLE IF NOT EXISTS v8_paper_missed (
    id SERIAL PRIMARY KEY, miss_date DATE NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, basket TEXT, expected_entry NUMERIC, target NUMERIC,
    stop_loss NUMERIC, reason TEXT, ts TIMESTAMP DEFAULT NOW(),
    UNIQUE (miss_date, symbol, side)
);
"""

def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(PAPER_SCHEMA_SQL); conn.commit()


# ============================================================ PIVOTS (nightly)
def compute_pivots(conn, for_date: date = None) -> Dict:
    ensure_schema(conn)
    for_date = for_date or date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active=TRUE ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]
    built, skipped = 0, []
    for sym in symbols:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT price_date, high, low, close FROM raw_prices
                WHERE symbol=%s AND price_date < %s ORDER BY price_date DESC LIMIT %s
            """, (sym, for_date, PIVOT_WINDOW))
            rows = [r for r in cur.fetchall() if r[1] and r[2] and r[3]]
        if len(rows) < PIVOT_MIN_DAYS:
            skipped.append(sym); continue
        wend, wstart = rows[0][0], rows[-1][0]
        bh = max(float(r[1]) for r in rows)
        bl = min(float(r[2]) for r in rows)
        bc = float(rows[0][3])
        pp = (bh + bl + bc) / 3.0
        r1 = 2*pp - bl; s1 = 2*pp - bh; r2 = pp + (bh-bl); s2 = pp - (bh-bl)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_paper_pivots
                (symbol,pivot_date,window_start,window_end,pp,r1,s1,r2,s2,
                 base_high,base_low,base_close,base_days,built_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (symbol,pivot_date) DO UPDATE SET
                  window_start=EXCLUDED.window_start,window_end=EXCLUDED.window_end,
                  pp=EXCLUDED.pp,r1=EXCLUDED.r1,s1=EXCLUDED.s1,r2=EXCLUDED.r2,s2=EXCLUDED.s2,
                  base_high=EXCLUDED.base_high,base_low=EXCLUDED.base_low,
                  base_close=EXCLUDED.base_close,base_days=EXCLUDED.base_days,built_at=NOW()
            """, (sym,for_date,wstart,wend,round(pp,2),round(r1,2),round(s1,2),
                  round(r2,2),round(s2,2),bh,bl,bc,len(rows)))
            conn.commit()
        built += 1
    log.info(f"paper pivots built {built}/{len(symbols)} for {for_date}")
    return {"pivot_date": str(for_date), "built": built, "total": len(symbols), "skipped": len(skipped)}


def _ensure_pivots_for(conn, d: date) -> int:
    """
    SELF-HEALING (12-Jun-2026): if no pivots exist for date d, compute them now.
    Returns the count of pivot rows present for d after the check. Cheap COUNT
    guard so this is effectively a no-op once the day's pivots exist (the first
    market-hours tick builds them; every later tick just sees them present).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM v8_paper_pivots WHERE pivot_date=%s", (d,))
        n = int(cur.fetchone()[0])
    if n == 0:
        log.info(f"paper pivots missing for {d} — self-healing build")
        res = compute_pivots(conn, d)
        return int(res.get("built", 0))
    return n


# ============================================================ QUALIFIED SET
def _passes(metric_row: Dict, bands: Dict) -> bool:
    for metric, bounds in bands.items():
        mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
        v = metric_row.get(metric)
        if v is None: return False
        v = float(v)
        if mn is not None and v < mn: return False
        if mx is not None and v > mx: return False
    return True

def qualified_set(conn) -> Dict[str, Dict]:
    from v8_endpoints import FILTER_CONFIG, BASKET_META
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, gvm_score, dma_20, dma_50, dma_200, rsi_month, rsi_weekly, daily_rsi,
                   month_return, week_return, year_return, mom_2d,
                   week_index_52, range_3d, ma9_vs_ma21, vol_ratio
            FROM v8_metrics WHERE score_date=(SELECT MAX(score_date) FROM v8_metrics)
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = {}
    for m in rows:
        sym = m["symbol"]
        for basket, filters in FILTER_CONFIG.items():
            # sell_overbought has its OWN entry model (precomputed entry/target/stop)
            # handled by _sell_overbought_signals + a dedicated entry branch; it does
            # NOT flow through the PP/R1/S1 zone path, so it is skipped here.
            if basket == "sell_overbought": continue
            side = BASKET_META[basket]["side"]
            if _passes(m, filters):
                out[sym] = {"basket": basket, "side": side}
                break
    return out


def _sell_overbought_signals(conn, for_date: date = None, force: bool = False) -> Dict[str, Dict]:
    """
    Cached accessor for sell_overbought signals (12-Jun-2026 follow-up).
    Signals are stable for the trading day, so this recomputes at most once per
    date. Pass force=True (or call invalidate_sell_overbought_cache()) to bust
    the cache after an intraday EOD-engine rerun.
    """
    d = for_date or date.today()
    if not force and _SO_CACHE["date"] == d and _SO_CACHE["signals"] is not None:
        return _SO_CACHE["signals"]
    sigs = _sell_overbought_signals_raw(conn)
    _SO_CACHE["date"] = d
    _SO_CACHE["signals"] = sigs
    log.info(f"sell_overbought signals computed for {d}: {len(sigs)} names (cached)")
    return sigs


def _sell_overbought_signals_raw(conn) -> Dict[str, Dict]:
    """
    sell_overbought signals — SQL lifted verbatim from v8_endpoints.sell_overbought
    (single logic, kept in sync). Returns {symbol: {"entry","target","stop"}} where:
      entry  = yesterday's close (signal anchor — NOT used as paper fill price),
      target = S1 (prev-day pivot, a fixed level the paper engine enters against),
      stop   = signal's own 1:1 stop (NOT used; paper recomputes off live entry).
    Paper entry uses target (S1) only; entry/stop are recomputed off the live
    5-min close at fill time (Option A, founder-locked 12-Jun-2026).
    """
    out = {}
    with conn.cursor() as cur:
        cur.execute("""
            WITH price_window AS (
                SELECT r.symbol, r.price_date, r.close, r.high, r.low, r.volume,
                       AVG(r.close) OVER w9 AS ma9, AVG(r.close) OVER w21 AS ma21,
                       AVG(r.volume) OVER w10 AS vol_avg10,
                       LAG(r.high,1) OVER ws AS prev_high, LAG(r.low,1) OVER ws AS prev_low,
                       LAG(r.close,1) OVER ws AS prev_close,
                       ROW_NUMBER() OVER (PARTITION BY r.symbol ORDER BY r.price_date DESC) AS rn
                FROM raw_prices r
                JOIN futures_universe fu ON fu.symbol = r.symbol AND fu.is_active = TRUE
                WHERE r.price_date >= CURRENT_DATE - INTERVAL '60 days'
                WINDOW w9 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 8 PRECEDING),
                       w21 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 20 PRECEDING),
                       w10 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 9 PRECEDING),
                       ws  AS (PARTITION BY r.symbol ORDER BY r.price_date)
            ),
            latest AS (
                SELECT pw.symbol, pw.close AS entry, pw.ma9, pw.ma21,
                       ROUND(((pw.ma9-pw.ma21)/NULLIF(pw.ma21,0)*100)::numeric,2) AS ma9_vs_ma21,
                       ROUND((pw.volume/NULLIF(pw.vol_avg10,0))::numeric,2) AS vol_ratio,
                       ROUND((((pw.prev_high+pw.prev_low+pw.prev_close)/3)-(pw.prev_high-(pw.prev_high+pw.prev_low+pw.prev_close)/3))::numeric,2) AS s1
                FROM price_window pw WHERE pw.rn=1 AND pw.ma21 IS NOT NULL AND pw.volume>0
            ),
            filtered AS (
                SELECT l.*, vm.dma_200, vm.week_index_52, vm.rsi_month,
                       vm.daily_rsi, vm.mom_2d, vm.gvm_score
                FROM latest l
                JOIN v8_metrics vm ON vm.symbol=l.symbol
                  AND vm.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                WHERE vm.dma_200>=10 AND vm.week_index_52>=80 AND l.ma9_vs_ma21>=3
                  AND l.vol_ratio<=0.8 AND vm.mom_2d<0 AND vm.rsi_month>=60
                  AND l.s1<l.entry
                  AND l.symbol NOT IN (SELECT UPPER(ticker) FROM earnings_calendar
                      WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE+INTERVAL '1 day'))
            )
            SELECT symbol, ROUND(entry::numeric,2) AS entry, s1 AS target,
                ROUND((entry+(entry-s1))::numeric,2) AS stop
            FROM filtered ORDER BY dma_200 DESC NULLS LAST
        """)
        for r in cur.fetchall():
            out[r[0]] = {"entry": float(r[1]), "target": float(r[2]), "stop": float(r[3])}
    return out


# ============================================================ HELPERS
def _two_latest_closes(conn, sym, d):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close, ts FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s AND ts::time BETWEEN '09:15' AND '15:30'
            ORDER BY ts DESC LIMIT 2
        """, (sym, d))
        rows = cur.fetchall()
    if len(rows) < 2: return None
    return float(rows[1][0]), float(rows[0][0]), rows[0][1]

def _latest_close(conn, sym, d):
    """Single most-recent 5-min close for d (used by sell_overbought entry,
    which needs only the current close, not the prev/cur pair)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close, ts FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s AND ts::time BETWEEN '09:15' AND '15:30'
            ORDER BY ts DESC LIMIT 1
        """, (sym, d))
        r = cur.fetchone()
    if not r: return None
    return float(r[0]), r[1]

def _first_bar(conn, sym, d):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT open, close, ts FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s AND ts::time BETWEEN '09:15' AND '15:30'
            ORDER BY ts ASC LIMIT 1
        """, (sym, d))
        r = cur.fetchone()
    return (float(r[0]) if r and r[0] is not None else (float(r[1]) if r else None),
            r[2] if r else None)

def _open_counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT side, COUNT(*) FROM v8_paper_positions WHERE status='OPEN' GROUP BY side")
        d = {r[0]: int(r[1]) for r in cur.fetchall()}
    return d.get("LONG", 0), d.get("SHORT", 0)

def _has_open(conn, sym, side):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side=%s AND status='OPEN'", (sym, side))
        return cur.fetchone() is not None

def _traded_today(conn, sym, side, d):
    """One entry per symbol/side/day — prevents zone re-entry after TARGET/SL."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM v8_paper_trades WHERE symbol=%s AND side=%s AND entry_ts::date=%s LIMIT 1", (sym, side, d))
        if cur.fetchone(): return True
        cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side=%s AND entry_ts::date=%s LIMIT 1", (sym, side, d))
        return cur.fetchone() is not None

def _lot(conn, sym):
    with conn.cursor() as cur:
        cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s", (sym,))
        r = cur.fetchone()
    return int(r[0]) if r and r[0] else 1

def _blackout(conn, sym):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM earnings_calendar
            WHERE UPPER(ticker)=%s AND ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
            LIMIT 1
        """, (sym.upper(),))
        return cur.fetchone() is not None

def _log_missed(conn, d, sym, side, basket, entry, target, sl, reason):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_paper_missed (miss_date,symbol,side,basket,expected_entry,target,stop_loss,reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (miss_date,symbol,side) DO NOTHING
        """, (d, sym, side, basket, round(entry,2), round(target,2), round(sl,2), reason))
        conn.commit()

def _close_position(conn, pid, sym, side, basket, entry, ets, qty, tgt, sl, pdt, exit_px, exit_ts, result):
    pnl = (exit_px-entry)*qty if side=="LONG" else (entry-exit_px)*qty
    ret = (exit_px/entry-1)*100 if side=="LONG" else (entry/exit_px-1)*100
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_paper_trades
            (symbol,side,basket,entry_price,entry_ts,exit_price,exit_ts,qty,target,stop_loss,
             pnl,return_pct,result,pivot_date,closed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        """, (sym,side,basket,entry,ets,exit_px,exit_ts,qty,tgt,sl,round(pnl,2),round(ret,2),result,pdt))
        cur.execute("DELETE FROM v8_paper_positions WHERE id=%s", (pid,))
        conn.commit()
    return {"symbol":sym,"side":side,"result":result,"exit":exit_px,"pnl":round(pnl,2)}


def _open_short(conn, sym, basket, entry, cur_ts, target, stop, pp, d):
    """Insert a SHORT paper position. Shared by zone-short and sell_overbought."""
    qty = _lot(conn, sym)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO v8_paper_positions
            (symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pp,pivot_date,status)
            VALUES (%s,'SHORT',%s,%s,%s,%s,%s,%s,%s,%s,'OPEN')
            ON CONFLICT (symbol,side,status) DO NOTHING""",
            (sym,basket,entry,cur_ts,qty,round(target,2),round(stop,2),pp,d))
        conn.commit()
    return {"symbol":sym,"side":"SHORT","basket":basket,"entry":entry,
            "target":round(target,2),"sl":round(stop,2)}


# ============================================================ 5-MIN TICK
def paper_tick(conn, target_date: date = None, buy_slots: int = None, sell_slots: int = None) -> Dict:
    ensure_schema(conn)
    d = target_date or date.today()
    now_t = datetime.now().time() if target_date is None else None

    # SELF-HEALING pivots (12-Jun-2026): build today's pivots if absent so the
    # engine never trades on prior-day pivots while waiting for the 22:05 job.
    _ensure_pivots_for(conn, d)

    with conn.cursor() as cur:
        cur.execute("SELECT symbol,pp,r1,s1 FROM v8_paper_pivots WHERE pivot_date=%s", (d,))
        piv = {r[0]:{"pp":float(r[1]),"r1":float(r[2]),"s1":float(r[3])} for r in cur.fetchall() if r[1] is not None}
    if not piv:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(pivot_date) FROM v8_paper_pivots")
            md = cur.fetchone()[0]
            if md:
                # DATE-DRIFT WARNING (12-Jun-2026 follow-up): self-heal should have
                # built pivots for d above; if we are here, today's build failed and
                # we are trading on OLDER pivots. Log loudly so staleness is visible
                # instead of silent. Convention itself (pivot_date=d, built from
                # price_date<d) is correct — this only flags a same-day build gap.
                if md < d:
                    log.warning(f"PIVOT DRIFT: no pivots for {d}, falling back to {md} "
                                f"({(d - md).days}d old) — self-heal build may have failed")
                cur.execute("SELECT symbol,pp,r1,s1 FROM v8_paper_pivots WHERE pivot_date=%s", (md,))
                piv = {r[0]:{"pp":float(r[1]),"r1":float(r[2]),"s1":float(r[3])} for r in cur.fetchall() if r[1] is not None}
    if not piv:
        return {"status":"warn","msg":"no pivots — run compute_pivots"}

    qual = qualified_set(conn)
    so_sig = _sell_overbought_signals(conn, for_date=d)   # cached, sell_overbought (own model)
    exits, entries = [], []

    # ---- 1) EXITS ----
    with conn.cursor() as cur:
        cur.execute("""SELECT id,symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pivot_date
                       FROM v8_paper_positions WHERE status='OPEN'""")
        open_rows = cur.fetchall()
    for (pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt) in open_rows:
        entry=float(entry); tgt=float(tgt); sl=float(sl); qty=int(qty)
        fb_open, fb_ts = _first_bar(conn, sym, d)
        if fb_open is not None and (ets is None or ets.date() < d):
            if side=="LONG":
                if fb_open>=tgt: exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_TARGET_EXIT")); continue
                if fb_open<=sl:  exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_SL_EXIT")); continue
            else:
                if fb_open<=tgt: exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_TARGET_EXIT")); continue
                if fb_open>=sl:  exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_SL_EXIT")); continue
        tl = _two_latest_closes(conn, sym, d)
        if not tl: continue
        _, cur_close, cur_ts = tl
        hit = None
        if side=="LONG":
            if cur_close>=tgt: hit="TARGET"
            elif cur_close<=sl: hit="SL"
        else:
            if cur_close<=tgt: hit="TARGET"
            elif cur_close>=sl: hit="SL"
        if hit:
            exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,cur_close,cur_ts,hit))

    # ---- 2) GATE REBALANCE ----
    rebalanced = []
    do_rebalance = (now_t is not None and now_t >= REBALANCE_TIME) or (target_date is not None)
    if do_rebalance and buy_slots is not None and sell_slots is not None:
        for side, cap in (("LONG", buy_slots), ("SHORT", sell_slots)):
            with conn.cursor() as cur:
                cur.execute("""SELECT id,symbol,basket,entry_price,entry_ts,qty,target,stop_loss,pivot_date
                               FROM v8_paper_positions WHERE status='OPEN' AND side=%s""", (side,))
                pos = cur.fetchall()
            excess = len(pos) - cap
            if excess <= 0: continue
            scored = []
            for (pid,sym,basket,entry,ets,qty,tgt,sl,pdt) in pos:
                tl = _two_latest_closes(conn, sym, d)
                if not tl: continue
                _, cur_close, cur_ts = tl
                entry=float(entry); qty=int(qty)
                upnl = (cur_close-entry)*qty if side=="LONG" else (entry-cur_close)*qty
                scored.append((upnl,pid,sym,basket,entry,ets,qty,float(tgt),float(sl),pdt,cur_close,cur_ts))
            best=sorted(scored,key=lambda x:-x[0]); worst=sorted(scored,key=lambda x:x[0])
            order,bi,wi,picked=[],0,0,set()
            while len(order)<excess and (bi<len(best) or wi<len(worst)):
                if bi<len(best) and best[bi][1] not in picked:
                    order.append(best[bi]); picked.add(best[bi][1]); bi+=1
                    if len(order)>=excess: break
                if wi<len(worst) and worst[wi][1] not in picked:
                    order.append(worst[wi]); picked.add(worst[wi][1]); wi+=1
                else: wi+=1
                if bi>=len(best) and wi>=len(worst): break
            for (upnl,pid,sym,basket,entry,ets,qty,tgt,sl,pdt,cur_close,cur_ts) in order[:excess]:
                rebalanced.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,cur_close,cur_ts,"GATE_EXIT"))

    # ---- 3) ENTRIES ----
    after_cutoff = (now_t is not None and now_t >= ENTRY_CUTOFF)
    long_open, short_open = _open_counts(conn)
    if not after_cutoff:
        for sym, q in qual.items():
            if sym not in piv: continue
            side = q["side"]; basket = q["basket"]
            pv = piv[sym]; pp,r1,s1 = pv["pp"],pv["r1"],pv["s1"]
            tl = _two_latest_closes(conn, sym, d)
            if not tl: continue
            prev_close, cur_close, cur_ts = tl
            # LONG entry: room-to-target fraction only (band condition removed 12-Jun-2026)
            if side=="LONG" and (r1 - cur_close) >= GAP_ROOM_FRAC * (r1 - pp):
                entry=cur_close; target=r1; stop=entry-(r1-entry)
                if _has_open(conn,sym,"LONG"): continue
                if _traded_today(conn,sym,"LONG",d): continue
                if _blackout(conn,sym):
                    _log_missed(conn,d,sym,"LONG",basket,entry,target,stop,"blackout"); continue
                if buy_slots is not None and long_open >= buy_slots:
                    _log_missed(conn,d,sym,"LONG",basket,entry,target,stop,"slot_full"); continue
                qty=_lot(conn,sym)
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO v8_paper_positions
                        (symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pp,pivot_date,status)
                        VALUES (%s,'LONG',%s,%s,%s,%s,%s,%s,%s,%s,'OPEN')
                        ON CONFLICT (symbol,side,status) DO NOTHING""",
                        (sym,basket,entry,cur_ts,qty,round(target,2),round(stop,2),pp,d))
                    conn.commit()
                long_open+=1
                entries.append({"symbol":sym,"side":"LONG","basket":basket,"entry":entry,"target":round(target,2),"sl":round(stop,2)})
            # SHORT entry: room-to-target fraction only (band condition removed 12-Jun-2026)
            elif side=="SHORT" and (cur_close - s1) >= GAP_ROOM_FRAC * (pp - s1):
                entry=cur_close; target=s1; stop=entry+(entry-s1)
                if _has_open(conn,sym,"SHORT"): continue
                if _traded_today(conn,sym,"SHORT",d): continue
                if _blackout(conn,sym):
                    _log_missed(conn,d,sym,"SHORT",basket,entry,target,stop,"blackout"); continue
                if sell_slots is not None and short_open >= sell_slots:
                    _log_missed(conn,d,sym,"SHORT",basket,entry,target,stop,"slot_full"); continue
                entries.append(_open_short(conn,sym,basket,entry,cur_ts,target,stop,pp,d))
                short_open+=1

        # ---- 3b) SELL_OVERBOUGHT ENTRIES (own model, 12-Jun-2026) ----
        # Enter SHORT at CURRENT 5-min close; target=S1 (signal), stop recomputed
        # off live entry for 1:1 (Option A). No price sanity gate ("enter regardless").
        for sym, sig in so_sig.items():
            target = sig["target"]                 # S1 (fixed pivot from signal)
            lc = _latest_close(conn, sym, d)
            if not lc: continue
            cur_close, cur_ts = lc
            if cur_close <= target:                # already at/below S1 — no trade room
                continue
            entry = cur_close
            stop  = entry + (entry - target)       # 1:1 off LIVE entry
            if _has_open(conn,sym,"SHORT"): continue
            if _traded_today(conn,sym,"SHORT",d): continue
            if _blackout(conn,sym):
                _log_missed(conn,d,sym,"SHORT","sell_overbought",entry,target,stop,"blackout"); continue
            if sell_slots is not None and short_open >= sell_slots:
                _log_missed(conn,d,sym,"SHORT","sell_overbought",entry,target,stop,"slot_full"); continue
            # pp stored as None for sell_overbought (no PP/R1/S1 zone anchor)
            entries.append(_open_short(conn,sym,"sell_overbought",entry,cur_ts,target,stop,None,d))
            short_open+=1
    else:
        for sym, q in qual.items():
            if sym not in piv: continue
            side=q["side"]; pv=piv[sym]; pp,r1,s1=pv["pp"],pv["r1"],pv["s1"]
            tl=_two_latest_closes(conn,sym,d)
            if not tl: continue
            prev_close,cur_close,_=tl
            # After-cutoff missed log — room fraction only (band condition removed 12-Jun-2026)
            if side=="LONG" and (r1-cur_close)>=GAP_ROOM_FRAC*(r1-pp) and not _has_open(conn,sym,"LONG") and not _traded_today(conn,sym,"LONG",d):
                _log_missed(conn,d,sym,"LONG",q["basket"],cur_close,r1,cur_close-(r1-cur_close),"after_cutoff")
            elif side=="SHORT" and (cur_close-s1)>=GAP_ROOM_FRAC*(pp-s1) and not _has_open(conn,sym,"SHORT") and not _traded_today(conn,sym,"SHORT",d):
                _log_missed(conn,d,sym,"SHORT",q["basket"],cur_close,s1,cur_close+(cur_close-s1),"after_cutoff")
        # sell_overbought after-cutoff missed log
        for sym, sig in so_sig.items():
            target = sig["target"]
            lc = _latest_close(conn, sym, d)
            if not lc: continue
            cur_close, _ = lc
            if cur_close <= target: continue
            if not _has_open(conn,sym,"SHORT") and not _traded_today(conn,sym,"SHORT",d):
                _log_missed(conn,d,sym,"SHORT","sell_overbought",cur_close,target,cur_close+(cur_close-target),"after_cutoff")

    return {"date":str(d),"qualified":len(qual),"sell_overbought":len(so_sig),"pivots":len(piv),
            "entries":entries,"exits":exits,"gate_exits":rebalanced,
            "open_long":long_open,"open_short":short_open}
