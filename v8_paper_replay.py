"""
v8_paper_replay.py — V8 Paper TRUE 5-min Stepped Replay (Scorr)
================================================================
08-Jun-2026. Isolated from the live engine (v8_paper.py) and from real trades.

Purpose
-------
Reconstruct what the V8 paper engine WOULD have done if run faithfully every
5-min since a start date, walking intraday_prices bar-by-bar.

Entry rule:
  LONG:  pp < cur_close <= r1  AND  (r1 - cur_close) >= 0.3 * (r1 - pp)
  SHORT: s1 <= cur_close < pp  AND  (cur_close - s1) >= 0.3 * (pp - s1)
  + _traded_today guard: one entry per symbol/side/day maximum.
  No prev_close condition — captures gap-down/up names opening in the zone.
  30% remaining gap ensures minimum reward room to target.
  NOTE (11-Jun-2026): lowered 0.5 -> 0.3 to match the live engine
  (v8_paper.py GAP_ROOM_FRAC=0.3). Kept hardcoded here to keep replay
  self-contained; update both files together if retuned.
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional, Dict, List, Tuple

log = logging.getLogger("scorr.v8replay")

PIVOT_WINDOW   = 5
PIVOT_MIN_DAYS = 3
ENTRY_CUTOFF   = time(15, 20)
REBALANCE_TIME = time(15, 20)
STEP_MINUTES   = 5
DAY_START      = time(9, 15)
DAY_END        = time(15, 30)


def _trading_days(conn, start: date, end: date) -> List[date]:
    from nse_holidays import is_trading_day
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ts::date FROM intraday_prices
            WHERE ts::date BETWEEN %s AND %s ORDER BY 1
        """, (start, end))
        return [r[0] for r in cur.fetchall() if is_trading_day(r[0])]


def wipe_book(conn) -> dict:
    counts = {}
    with conn.cursor() as cur:
        for tbl in ("v8_paper_positions", "v8_paper_trades", "v8_paper_missed"):
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            counts[tbl] = cur.fetchone()[0]
            cur.execute(f"DELETE FROM {tbl}")
        conn.commit()
    return counts


def build_pivots_for_day(conn, for_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active=TRUE ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]
    built = 0
    for sym in symbols:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT price_date, high, low, close FROM raw_prices
                WHERE symbol=%s AND price_date < %s ORDER BY price_date DESC LIMIT %s
            """, (sym, for_date, PIVOT_WINDOW))
            rows = [r for r in cur.fetchall() if r[1] and r[2] and r[3]]
        if len(rows) < PIVOT_MIN_DAYS: continue
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
    return built


def _pivots_for(conn, d: date) -> Dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol,pp,r1,s1 FROM v8_paper_pivots WHERE pivot_date=%s", (d,))
        return {r[0]: {"pp": float(r[1]), "r1": float(r[2]), "s1": float(r[3])}
                for r in cur.fetchall() if r[1] is not None}


def _passes_band(value, mn, mx) -> bool:
    if value is None: return False
    v = float(value)
    if mn is not None and v < mn: return False
    if mx is not None and v > mx: return False
    return True


def _gate_threshold(fails: int, n_filters: int) -> int:
    if fails <= 1: return n_filters
    if fails == 2: return n_filters - 1
    return n_filters - 2


def qualified_for_day(conn, d: date, gate_fails: int) -> Dict[str, dict]:
    from v8_endpoints import FILTER_CONFIG, BASKET_META
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, gvm_score, dma_20, dma_50, dma_200, rsi_month, rsi_weekly, daily_rsi,
                   month_return, week_return, year_return, day_change,
                   week_index_52, range_3d, ma9_vs_ma21, vol_ratio, sector_week, sector_month
            FROM v8_metrics WHERE score_date=%s
        """, (d,))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    basket_members: Dict[str, set] = {}
    for basket, filters in FILTER_CONFIG.items():
        if basket == "sell_overbought": continue
        members = set()
        if basket == "buy_reversal":
            need = _gate_threshold(gate_fails, len(filters))
            for m in rows:
                dc = m.get("day_change")
                if dc is None or float(dc) <= 0: continue
                passed = sum(1 for metric, bounds in filters.items()
                             if _passes_band(m.get(metric), *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1]))))
                if passed >= need: members.add(m["symbol"])
        else:
            for m in rows:
                if all(_passes_band(m.get(metric), *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1])))
                       for metric, bounds in filters.items()):
                    members.add(m["symbol"])
        basket_members[basket] = members

    out: Dict[str, dict] = {}
    for m in rows:
        sym = m["symbol"]
        for basket in FILTER_CONFIG:
            if basket == "sell_overbought": continue
            if sym in basket_members[basket]:
                side = "LONG" if BASKET_META[basket]["side"] == "BUY" else "SHORT"
                out[sym] = {"basket": basket, "side": side}
                break
    return out


def gate_slots_for_day(conn, d: date) -> Tuple[int, int, int]:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH li AS (SELECT DISTINCT ON (symbol) symbol, close AS cmp
                            FROM intraday_prices WHERE ts::date=%s ORDER BY symbol, ts DESC),
                     pc AS (SELECT DISTINCT ON (symbol) symbol, close AS pclose
                            FROM raw_prices WHERE price_date < %s ORDER BY symbol, price_date DESC)
                SELECT COUNT(*) FILTER (WHERE li.cmp > pc.pclose),
                       COUNT(*) FILTER (WHERE li.cmp < pc.pclose), COUNT(*)
                FROM li JOIN pc ON pc.symbol=li.symbol
            """, (d, d))
            adv, dec, tot = cur.fetchone()
            adr = (adv/dec) if (tot and tot>=50 and dec) else (float(adv) if (tot and tot>=50) else 1.0)
            cur.execute("SELECT close FROM intraday_prices WHERE symbol='NIFTY50' AND ts::date=%s ORDER BY ts DESC LIMIT 1", (d,))
            lv = cur.fetchone()
            cur.execute("SELECT close FROM raw_prices WHERE symbol='NIFTY50' AND price_date < %s ORDER BY price_date DESC LIMIT 30", (d,))
            hist = [float(x[0]) for x in cur.fetchall()]
            if lv and lv[0] is not None and len(hist) >= 22:
                latest = float(lv[0])
                nday = (latest/hist[0]-1)*100; nweek = (latest/hist[4]-1)*100; nmonth = (latest/hist[20]-1)*100
            else:
                nday = nweek = nmonth = 0.0
            fails = sum(1 for c in [adr>=1.0, nday>=0, nweek>=0, nmonth>=0] if not c)
    except Exception as e:
        log.warning(f"gate_slots_for_day {d}: {e}"); fails = 2
    if fails == 0:   return 10, 5, fails
    if fails == 1:   return 8, 7, fails
    if fails == 2:   return 7, 8, fails
    return 5, 10, fails


def _two_closes_upto(conn, sym: str, d: date, cutoff: time):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close, ts FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s AND ts::time BETWEEN %s AND %s
            ORDER BY ts DESC LIMIT 2
        """, (sym, d, DAY_START, cutoff))
        rows = cur.fetchall()
    if len(rows) < 2: return None
    return float(rows[1][0]), float(rows[0][0]), rows[0][1]


def _first_bar(conn, sym: str, d: date):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT open, close, ts FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s AND ts::time BETWEEN %s AND %s ORDER BY ts ASC LIMIT 1
        """, (sym, d, DAY_START, DAY_END))
        r = cur.fetchone()
    if not r: return None, None
    return (float(r[0]) if r[0] is not None else float(r[1])), r[2]


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


def _close_position(conn, pid, sym, side, basket, entry, ets, qty, tgt, sl, pdt, exit_px, exit_ts, result):
    pnl = (exit_px-entry)*qty if side=="LONG" else (entry-exit_px)*qty
    ret = (exit_px/entry-1)*100 if side=="LONG" else (entry/exit_px-1)*100
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_paper_trades
            (symbol,side,basket,entry_price,entry_ts,exit_price,exit_ts,qty,target,stop_loss,
             pnl,return_pct,result,pivot_date,closed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,(NOW() AT TIME ZONE 'Asia/Kolkata'))  -- cc#325: naive IST
        """, (sym,side,basket,entry,ets,exit_px,exit_ts,qty,tgt,sl,round(pnl,2),round(ret,2),result,pdt))
        cur.execute("DELETE FROM v8_paper_positions WHERE id=%s", (pid,))
        conn.commit()
    return {"symbol": sym, "side": side, "result": result, "exit": exit_px, "pnl": round(pnl, 2)}


def _replay_tick(conn, d: date, cutoff: time, qual: Dict[str, dict],
                 piv: Dict[str, dict], buy_slots: int, sell_slots: int,
                 is_new_day: bool) -> dict:
    exits, entries, gate_exits = [], [], []

    # ---- 1) EXITS ----
    with conn.cursor() as cur:
        cur.execute("""SELECT id,symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pivot_date
                       FROM v8_paper_positions WHERE status='OPEN'""")
        open_rows = cur.fetchall()
    for (pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt) in open_rows:
        entry=float(entry); tgt=float(tgt); sl=float(sl); qty=int(qty)
        if is_new_day and (ets is None or ets.date() < d):
            fb_open, fb_ts = _first_bar(conn, sym, d)
            if fb_open is not None:
                if side=="LONG":
                    if fb_open>=tgt: exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_TARGET_EXIT")); continue
                    if fb_open<=sl:  exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_SL_EXIT")); continue
                else:
                    if fb_open<=tgt: exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_TARGET_EXIT")); continue
                    if fb_open>=sl:  exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_SL_EXIT")); continue
        tl = _two_closes_upto(conn, sym, d, cutoff)
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
    if cutoff >= REBALANCE_TIME:
        for side, cap in (("LONG", buy_slots), ("SHORT", sell_slots)):
            with conn.cursor() as cur:
                cur.execute("""SELECT id,symbol,basket,entry_price,entry_ts,qty,target,stop_loss,pivot_date
                               FROM v8_paper_positions WHERE status='OPEN' AND side=%s""", (side,))
                pos = cur.fetchall()
            excess = len(pos) - cap
            if excess <= 0: continue
            scored = []
            for (pid,sym,basket,entry,ets,qty,tgt,sl,pdt) in pos:
                tl = _two_closes_upto(conn, sym, d, cutoff)
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
                gate_exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,cur_close,cur_ts,"GATE_EXIT"))

    # ---- 3) ENTRIES ----
    if cutoff < ENTRY_CUTOFF:
        long_open, short_open = _open_counts(conn)
        for sym, q in qual.items():
            if sym not in piv: continue
            side = q["side"]; basket = q["basket"]
            pv = piv[sym]; pp, r1, s1 = pv["pp"], pv["r1"], pv["s1"]
            tl = _two_closes_upto(conn, sym, d, cutoff)
            if not tl: continue
            _, cur_close, cur_ts = tl
            if side == "LONG" and (pp < cur_close <= r1) and (r1 - cur_close) >= 0.3 * (r1 - pp):
                if _has_open(conn, sym, "LONG"): continue
                if _traded_today(conn, sym, "LONG", d): continue
                if long_open >= buy_slots: continue
                entry = cur_close; target = r1; stop = entry - (r1 - entry)
                qty = _lot(conn, sym)
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO v8_paper_positions
                        (symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pp,pivot_date,status)
                        VALUES (%s,'LONG',%s,%s,%s,%s,%s,%s,%s,%s,'OPEN')
                        ON CONFLICT (symbol,side,status) DO NOTHING""",
                        (sym,basket,entry,cur_ts,qty,round(target,2),round(stop,2),pp,d))
                    conn.commit()
                long_open += 1
                entries.append({"symbol": sym, "side": "LONG", "basket": basket, "entry": entry})
            elif side == "SHORT" and (s1 <= cur_close < pp) and (cur_close - s1) >= 0.3 * (pp - s1):
                if _has_open(conn, sym, "SHORT"): continue
                if _traded_today(conn, sym, "SHORT", d): continue
                if short_open >= sell_slots: continue
                entry = cur_close; target = s1; stop = entry + (entry - s1)
                qty = _lot(conn, sym)
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO v8_paper_positions
                        (symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pp,pivot_date,status)
                        VALUES (%s,'SHORT',%s,%s,%s,%s,%s,%s,%s,%s,'OPEN')
                        ON CONFLICT (symbol,side,status) DO NOTHING""",
                        (sym,basket,entry,cur_ts,qty,round(target,2),round(stop,2),pp,d))
                    conn.commit()
                short_open += 1
                entries.append({"symbol": sym, "side": "SHORT", "basket": basket, "entry": entry})

    return {"entries": entries, "exits": exits, "gate_exits": gate_exits}


def _step_times() -> List[time]:
    out = []
    cur = datetime(2000,1,1, DAY_START.hour, DAY_START.minute)
    end = datetime(2000,1,1, DAY_END.hour, DAY_END.minute)
    while cur <= end:
        out.append(cur.time())
        cur += timedelta(minutes=STEP_MINUTES)
    return out


def run_replay(conn, start: date, end: date = None, wipe: bool = True) -> dict:
    end = end or date.today()
    summary = {"start": str(start), "end": str(end), "wiped": None, "days": [], "totals": {}}
    if wipe:
        summary["wiped"] = wipe_book(conn)
    days = _trading_days(conn, start, end)
    summary["trading_days"] = [str(d) for d in days]
    steps = _step_times()
    tot_entries = tot_exits = tot_gate = 0
    qual_sizes = []
    for d in days:
        build_pivots_for_day(conn, d)
        piv = _pivots_for(conn, d)
        buy_slots, sell_slots, fails = gate_slots_for_day(conn, d)
        qual = qualified_for_day(conn, d, fails)
        n_long  = sum(1 for q in qual.values() if q["side"] == "LONG")
        n_short = sum(1 for q in qual.values() if q["side"] == "SHORT")
        qual_sizes.append(len(qual))
        d_entries = d_exits = d_gate = 0
        for i, cutoff in enumerate(steps):
            res = _replay_tick(conn, d, cutoff, qual, piv, buy_slots, sell_slots, is_new_day=(i==0))
            d_entries += len(res["entries"]); d_exits += len(res["exits"]); d_gate += len(res["gate_exits"])
        summary["days"].append({
            "date": str(d), "pivots": len(piv), "gate_fails": fails,
            "buy_slots": buy_slots, "sell_slots": sell_slots,
            "qualified": len(qual), "qual_long": n_long, "qual_short": n_short,
            "entries": d_entries, "exits": d_exits, "gate_exits": d_gate,
        })
        tot_entries += d_entries; tot_exits += d_exits; tot_gate += d_gate
    summary["qualified_universe"] = max(qual_sizes) if qual_sizes else 0
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM v8_paper_positions WHERE status='OPEN'")
        open_n = cur.fetchone()[0]
        cur.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE pnl>0),
                       COALESCE(ROUND(SUM(pnl)::numeric,2),0), COALESCE(ROUND(AVG(return_pct)::numeric,2),0)
                       FROM v8_paper_trades""")
        n, wins, total_pnl, avg_ret = cur.fetchone()
    summary["totals"] = {
        "entries": tot_entries, "exits": tot_exits, "gate_exits": tot_gate,
        "closed_trades": n, "wins": wins,
        "win_rate_pct": round(wins/n*100, 1) if n else 0.0,
        "total_pnl": float(total_pnl), "avg_return_pct": float(avg_ret),
        "open_positions": open_n,
    }
    return summary
