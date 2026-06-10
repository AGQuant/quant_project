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
  3. Zone + gap trigger — on 1-min candle close, on qualified names only.

ENTRY (qualified name + zone + gap condition + free slot + not blackout + before 15:20):
  BUY  : pp < this_close <= r1  AND  (r1-this_close) >= 0.5*(r1-pp)
         -> enter @ close, target R1, SL = entry-(R1-entry)
  SHORT: s1 <= this_close < pp  AND  (this_close-s1) >= 0.5*(pp-s1)
         -> enter @ close, target S1, SL = entry+(entry-S1)
  + _traded_today guard: one entry per symbol/side/day maximum.
  No prev_close condition — captures gap-down/up names opening in the zone.
  50% remaining gap ensures minimum reward room to target.

EXIT (close-based, multi-day, levels frozen at entry):
  target hit / SL hit -> TARGET / SL
  gap at first bar of day past level -> GAP_TARGET_EXIT / GAP_SL_EXIT (exit at open)
  gate rebalance once at 15:20 both sides: open > current slots -> close excess
     order best-profit, worst-loss, 2nd-best, 2nd-worst... stop when enough -> GATE_EXIT

SIZING : 1 lot (futures_universe.lot_size, default 1).
GATE   : market-mood buy_slots/sell_slots (sum 15) cap concurrent open per side.
MISSED : qualified + zone trigger but blocked by slot_full|blackout|after_cutoff ->
         v8_paper_missed, one row per (symbol, side, day).

Price feed: intraday_prices (Fyers 1-min, NAIVE IST ts — read RAW, no TZ math).

FILTER_CONFIG: imported from v8_endpoints (canonical). Do NOT duplicate here.
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional, Dict, List

log = logging.getLogger("scorr.v8paper")

PIVOT_WINDOW   = 5
PIVOT_MIN_DAYS = 3
ENTRY_CUTOFF   = time(15, 20)
REBALANCE_TIME = time(15, 20)


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
            if basket == "sell_overbought": continue
            side = BASKET_META[basket]["side"]
            if _passes(m, filters):
                out[sym] = {"basket": basket, "side": side}
                break
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


# ============================================================ 1-MIN TICK
def paper_tick(conn, target_date: date = None, buy_slots: int = None, sell_slots: int = None) -> Dict:
    ensure_schema(conn)
    d = target_date or date.today()
    now_t = datetime.now().time() if target_date is None else None

    with conn.cursor() as cur:
        cur.execute("SELECT symbol,pp,r1,s1 FROM v8_paper_pivots WHERE pivot_date=%s", (d,))
        piv = {r[0]:{"pp":float(r[1]),"r1":float(r[2]),"s1":float(r[3])} for r in cur.fetchall() if r[1] is not None}
    if not piv:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(pivot_date) FROM v8_paper_pivots")
            md = cur.fetchone()[0]
            if md:
                cur.execute("SELECT symbol,pp,r1,s1 FROM v8_paper_pivots WHERE pivot_date=%s", (md,))
                piv = {r[0]:{"pp":float(r[1]),"r1":float(r[2]),"s1":float(r[3])} for r in cur.fetchall() if r[1] is not None}
    if not piv:
        return {"status":"warn","msg":"no pivots — run compute_pivots"}

    qual = qualified_set(conn)
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
            if side=="LONG" and (pp < cur_close <= r1) and (r1 - cur_close) >= 0.5 * (r1 - pp):
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
            elif side=="SHORT" and (s1 <= cur_close < pp) and (cur_close - s1) >= 0.5 * (pp - s1):
                entry=cur_close; target=s1; stop=entry+(entry-s1)
                if _has_open(conn,sym,"SHORT"): continue
                if _traded_today(conn,sym,"SHORT",d): continue
                if _blackout(conn,sym):
                    _log_missed(conn,d,sym,"SHORT",basket,entry,target,stop,"blackout"); continue
                if sell_slots is not None and short_open >= sell_slots:
                    _log_missed(conn,d,sym,"SHORT",basket,entry,target,stop,"slot_full"); continue
                qty=_lot(conn,sym)
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO v8_paper_positions
                        (symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pp,pivot_date,status)
                        VALUES (%s,'SHORT',%s,%s,%s,%s,%s,%s,%s,%s,'OPEN')
                        ON CONFLICT (symbol,side,status) DO NOTHING""",
                        (sym,basket,entry,cur_ts,qty,round(target,2),round(stop,2),pp,d))
                    conn.commit()
                short_open+=1
                entries.append({"symbol":sym,"side":"SHORT","basket":basket,"entry":entry,"target":round(target,2),"sl":round(stop,2)})
    else:
        for sym, q in qual.items():
            if sym not in piv: continue
            side=q["side"]; pv=piv[sym]; pp,r1,s1=pv["pp"],pv["r1"],pv["s1"]
            tl=_two_latest_closes(conn,sym,d)
            if not tl: continue
            prev_close,cur_close,_=tl
            if side=="LONG" and (pp < cur_close <= r1) and (r1-cur_close)>=0.5*(r1-pp) and not _has_open(conn,sym,"LONG") and not _traded_today(conn,sym,"LONG",d):
                _log_missed(conn,d,sym,"LONG",q["basket"],cur_close,r1,cur_close-(r1-cur_close),"after_cutoff")
            elif side=="SHORT" and (s1<=cur_close<pp) and (cur_close-s1)>=0.5*(pp-s1) and not _has_open(conn,sym,"SHORT") and not _traded_today(conn,sym,"SHORT",d):
                _log_missed(conn,d,sym,"SHORT",q["basket"],cur_close,s1,cur_close+(cur_close-s1),"after_cutoff")

    return {"date":str(d),"qualified":len(qual),"pivots":len(piv),
            "entries":entries,"exits":exits,"gate_exits":rebalanced,
            "open_long":long_open,"open_short":short_open}
