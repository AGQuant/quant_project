"""
v8_paper_replay.py — V8 Paper TRUE 5-min Stepped Replay (Scorr)
================================================================
08-Jun-2026. Isolated from the live engine (v8_paper.py) and from real trades.

Purpose
-------
Reconstruct what the V8 paper engine WOULD have done if run faithfully every
5-min since a start date, walking intraday_prices bar-by-bar. The live engine
had a writer stall (08-Jun 09:40) and earlier gaps, so the live v8_paper_* book
is incomplete. This module replays cleanly from a wiped book.

Design
------
- Universe / sides: today's qualified set (proxy), via FILTER_CONFIG on latest
  v8_metrics. (Per user decision 08-Jun: today's qualified as proxy — faster.)
- Pivots: rolling-5-day from raw_prices, recomputed PER replay day (window rolls).
- Stepped walk: for each trading day, step a cutoff time 09:15 → 15:30 in 5-min
  increments. At each cutoff, EXACTLY the live engine's entry/exit logic runs,
  but every intraday read is BOUNDED to ts <= cutoff (true point-in-time replay,
  not "latest bar of day"). Positions carry across days (multi-day swing).
- Gate slots: computed per replay day from that day's breadth + Nifty D/W/M
  (mirrors /api/v8/market_mood thresholds). Falls back to 7/8 (Neutral).
- Entry cutoff 15:20, gate rebalance once at the >=15:20 step. Same as live.

Writes the SAME tables as the live engine so the dashboard reads it natively:
  v8_paper_positions / v8_paper_trades / v8_paper_missed / v8_paper_pivots

After replay, tomorrow's live loop appends real ticks onto this seeded book.

NOTE: wipe_book() is destructive — clears v8_paper_positions/trades/missed.
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


# ── trading-day discovery ────────────────────────────────────────────────────

def _trading_days(conn, start: date, end: date) -> List[date]:
    """
    Distinct dates in intraday_prices within [start, end] that are ALSO valid NSE
    trading days, ascending. Gating through is_trading_day excludes weekends and
    holidays — intraday_prices can contain stray non-trading-day bars (feed
    artifacts / global-symbol bleed) which must NOT be replayed (08-Jun-2026 fix:
    Saturday 06-Jun had 211-symbol bars and contaminated the first replay run).
    """
    from nse_holidays import is_trading_day
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ts::date
            FROM intraday_prices
            WHERE ts::date BETWEEN %s AND %s
            ORDER BY 1
        """, (start, end))
        return [r[0] for r in cur.fetchall() if is_trading_day(r[0])]


# ── wipe ─────────────────────────────────────────────────────────────────────

def wipe_book(conn) -> dict:
    """DESTRUCTIVE: clear the paper book so replay starts from empty."""
    counts = {}
    with conn.cursor() as cur:
        for tbl in ("v8_paper_positions", "v8_paper_trades", "v8_paper_missed"):
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            counts[tbl] = cur.fetchone()[0]
            cur.execute(f"DELETE FROM {tbl}")
        conn.commit()
    log.info(f"replay wipe_book: cleared {counts}")
    return counts


# ── pivots per replay day (rolling-5 from raw_prices) ────────────────────────

def build_pivots_for_day(conn, for_date: date) -> int:
    """Rolling-5-day pivots applying to for_date, from last 5 sessions < for_date."""
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active=TRUE ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]
    built = 0
    for sym in symbols:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT price_date, high, low, close FROM raw_prices
                WHERE symbol=%s AND price_date < %s
                ORDER BY price_date DESC LIMIT %s
            """, (sym, for_date, PIVOT_WINDOW))
            rows = [r for r in cur.fetchall()
                    if r[1] is not None and r[2] is not None and r[3] is not None]
        if len(rows) < PIVOT_MIN_DAYS:
            continue
        wend, wstart = rows[0][0], rows[-1][0]
        bh = max(float(r[1]) for r in rows)
        bl = min(float(r[2]) for r in rows)
        bc = float(rows[0][3])
        pp = (bh + bl + bc) / 3.0
        r1 = 2*pp - bl; s1 = 2*pp - bh; r2 = pp + (bh - bl); s2 = pp - (bh - bl)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_paper_pivots
                (symbol,pivot_date,window_start,window_end,pp,r1,s1,r2,s2,
                 base_high,base_low,base_close,base_days,built_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (symbol,pivot_date) DO UPDATE SET
                  window_start=EXCLUDED.window_start, window_end=EXCLUDED.window_end,
                  pp=EXCLUDED.pp, r1=EXCLUDED.r1, s1=EXCLUDED.s1, r2=EXCLUDED.r2, s2=EXCLUDED.s2,
                  base_high=EXCLUDED.base_high, base_low=EXCLUDED.base_low,
                  base_close=EXCLUDED.base_close, base_days=EXCLUDED.base_days, built_at=NOW()
            """, (sym, for_date, wstart, wend, round(pp,2), round(r1,2), round(s1,2),
                  round(r2,2), round(s2,2), bh, bl, bc, len(rows)))
            conn.commit()
        built += 1
    return built


def _pivots_for(conn, d: date) -> Dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol,pp,r1,s1 FROM v8_paper_pivots WHERE pivot_date=%s", (d,))
        return {r[0]: {"pp": float(r[1]), "r1": float(r[2]), "s1": float(r[3])}
                for r in cur.fetchall() if r[1] is not None}


# ── qualified proxy (today's set via FILTER_CONFIG on latest v8_metrics) ─────

def qualified_proxy(conn) -> Dict[str, dict]:
    from v8_endpoints import FILTER_CONFIG, BASKET_META

    def _passes(m, bands):
        for metric, bounds in bands.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            v = m.get(metric)
            if v is None:
                return False
            v = float(v)
            if mn is not None and v < mn: return False
            if mx is not None and v > mx: return False
        return True

    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, gvm_score, dma_20, dma_50, dma_200, rsi_month, rsi_weekly, daily_rsi,
                   month_return, week_return, year_return, day_change,
                   week_index_52, range_3d, ma9_vs_ma21, vol_ratio,
                   sector_week, sector_month
            FROM v8_metrics WHERE score_date=(SELECT MAX(score_date) FROM v8_metrics)
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    out = {}
    for m in rows:
        sym = m["symbol"]
        for basket, filters in FILTER_CONFIG.items():
            if basket == "sell_overbought":
                continue
            side = "LONG" if BASKET_META[basket]["side"] == "BUY" else "SHORT"
            if _passes(m, filters):
                out[sym] = {"basket": basket, "side": side}
                break
    return out


# ── per-day market gate (slots), mirrors /api/v8/market_mood thresholds ───────

def gate_slots_for_day(conn, d: date) -> Tuple[int, int, int]:
    """Returns (buy_slots, sell_slots, fails) computed as of replay day d."""
    try:
        with conn.cursor() as cur:
            # breadth: intraday-as-of-d last close vs prev EOD close
            cur.execute("""
                WITH li AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS cmp
                    FROM intraday_prices WHERE ts::date = %s
                    ORDER BY symbol, ts DESC
                ),
                pc AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS pclose
                    FROM raw_prices WHERE price_date < %s
                    ORDER BY symbol, price_date DESC
                )
                SELECT COUNT(*) FILTER (WHERE li.cmp > pc.pclose),
                       COUNT(*) FILTER (WHERE li.cmp < pc.pclose),
                       COUNT(*)
                FROM li JOIN pc ON pc.symbol = li.symbol
            """, (d, d))
            adv, dec, tot = cur.fetchone()
            adr = (adv / dec) if (tot and tot >= 50 and dec) else (float(adv) if (tot and tot >= 50) else 1.0)

            cur.execute("""
                SELECT close FROM intraday_prices
                WHERE symbol='NIFTY50' AND ts::date=%s ORDER BY ts DESC LIMIT 1
            """, (d,))
            lv = cur.fetchone()
            cur.execute("""
                SELECT close FROM raw_prices
                WHERE symbol='NIFTY50' AND price_date < %s
                ORDER BY price_date DESC LIMIT 30
            """, (d,))
            hist = [float(x[0]) for x in cur.fetchall()]
            if lv and lv[0] is not None and len(hist) >= 22:
                latest = float(lv[0])
                nday   = (latest / hist[0]  - 1) * 100
                nweek  = (latest / hist[4]  - 1) * 100
                nmonth = (latest / hist[20] - 1) * 100
            else:
                nday = nweek = nmonth = 0.0

            checks = [adr >= 1.0, nday >= 0, nweek >= 0, nmonth >= 0]
            fails = sum(1 for c in checks if not c)
    except Exception as e:
        log.warning(f"gate_slots_for_day {d}: {e}")
        fails = 2

    if fails == 0:   return 10, 5, fails
    if fails == 1:   return 8, 7, fails
    if fails == 2:   return 7, 8, fails
    return 5, 10, fails


# ── time-bounded intraday reads (point-in-time) ──────────────────────────────

def _two_closes_upto(conn, sym: str, d: date, cutoff: time):
    """Latest 2 closes for sym on day d with ts::time <= cutoff. (prev, cur, cur_ts)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close, ts FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s
              AND ts::time BETWEEN %s AND %s
            ORDER BY ts DESC LIMIT 2
        """, (sym, d, DAY_START, cutoff))
        rows = cur.fetchall()
    if len(rows) < 2:
        return None
    return float(rows[1][0]), float(rows[0][0]), rows[0][1]


def _first_bar(conn, sym: str, d: date):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT open, close, ts FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s AND ts::time BETWEEN %s AND %s
            ORDER BY ts ASC LIMIT 1
        """, (sym, d, DAY_START, DAY_END))
        r = cur.fetchone()
    if not r:
        return None, None
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


def _lot(conn, sym):
    with conn.cursor() as cur:
        cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s", (sym,))
        r = cur.fetchone()
    return int(r[0]) if r and r[0] else 1


def _close_position(conn, pid, sym, side, basket, entry, ets, qty, tgt, sl, pdt, exit_px, exit_ts, result):
    pnl = (exit_px-entry)*qty if side == "LONG" else (entry-exit_px)*qty
    ret = (exit_px/entry-1)*100 if side == "LONG" else (entry/exit_px-1)*100
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_paper_trades
            (symbol,side,basket,entry_price,entry_ts,exit_price,exit_ts,qty,target,stop_loss,
             pnl,return_pct,result,pivot_date,closed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        """, (sym,side,basket,entry,ets,exit_px,exit_ts,qty,tgt,sl,round(pnl,2),round(ret,2),result,pdt))
        cur.execute("DELETE FROM v8_paper_positions WHERE id=%s", (pid,))
        conn.commit()
    return {"symbol": sym, "side": side, "result": result, "exit": exit_px, "pnl": round(pnl, 2)}


# ── one stepped tick at (d, cutoff) ──────────────────────────────────────────

def _replay_tick(conn, d: date, cutoff: time, qual: Dict[str, dict],
                 piv: Dict[str, dict], buy_slots: int, sell_slots: int,
                 is_new_day: bool) -> dict:
    exits, entries, gate_exits = [], [], []

    # ---- 1) EXITS (gap exits only on first step of a new day) ----
    with conn.cursor() as cur:
        cur.execute("""SELECT id,symbol,side,basket,entry_price,entry_ts,qty,target,stop_loss,pivot_date
                       FROM v8_paper_positions WHERE status='OPEN'""")
        open_rows = cur.fetchall()
    for (pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt) in open_rows:
        entry=float(entry); tgt=float(tgt); sl=float(sl); qty=int(qty)
        if is_new_day and (ets is None or ets.date() < d):
            fb_open, fb_ts = _first_bar(conn, sym, d)
            if fb_open is not None:
                if side == "LONG":
                    if fb_open >= tgt: exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_TARGET_EXIT")); continue
                    if fb_open <= sl:  exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_SL_EXIT")); continue
                else:
                    if fb_open <= tgt: exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_TARGET_EXIT")); continue
                    if fb_open >= sl:  exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,fb_open,fb_ts,"GAP_SL_EXIT")); continue
        tl = _two_closes_upto(conn, sym, d, cutoff)
        if not tl:
            continue
        _, cur_close, cur_ts = tl
        hit = None
        if side == "LONG":
            if cur_close >= tgt: hit = "TARGET"
            elif cur_close <= sl: hit = "SL"
        else:
            if cur_close <= tgt: hit = "TARGET"
            elif cur_close >= sl: hit = "SL"
        if hit:
            exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,cur_close,cur_ts,hit))

    # ---- 2) GATE REBALANCE (once, at first step >= 15:20) ----
    if cutoff >= REBALANCE_TIME:
        for side, cap in (("LONG", buy_slots), ("SHORT", sell_slots)):
            with conn.cursor() as cur:
                cur.execute("""SELECT id,symbol,basket,entry_price,entry_ts,qty,target,stop_loss,pivot_date
                               FROM v8_paper_positions WHERE status='OPEN' AND side=%s""", (side,))
                pos = cur.fetchall()
            excess = len(pos) - cap
            if excess <= 0:
                continue
            scored = []
            for (pid,sym,basket,entry,ets,qty,tgt,sl,pdt) in pos:
                tl = _two_closes_upto(conn, sym, d, cutoff)
                if not tl:
                    continue
                _, cur_close, cur_ts = tl
                entry=float(entry); qty=int(qty)
                upnl = (cur_close-entry)*qty if side == "LONG" else (entry-cur_close)*qty
                scored.append((upnl,pid,sym,basket,entry,ets,qty,float(tgt),float(sl),pdt,cur_close,cur_ts))
            best  = sorted(scored, key=lambda x: -x[0])
            worst = sorted(scored, key=lambda x:  x[0])
            order, bi, wi, picked = [], 0, 0, set()
            while len(order) < excess and (bi < len(best) or wi < len(worst)):
                if bi < len(best) and best[bi][1] not in picked:
                    order.append(best[bi]); picked.add(best[bi][1]); bi += 1
                    if len(order) >= excess: break
                if wi < len(worst) and worst[wi][1] not in picked:
                    order.append(worst[wi]); picked.add(worst[wi][1]); wi += 1
                else:
                    wi += 1
                if bi >= len(best) and wi >= len(worst): break
            for (upnl,pid,sym,basket,entry,ets,qty,tgt,sl,pdt,cur_close,cur_ts) in order[:excess]:
                gate_exits.append(_close_position(conn,pid,sym,side,basket,entry,ets,qty,tgt,sl,pdt,cur_close,cur_ts,"GATE_EXIT"))

    # ---- 3) ENTRIES (skip after cutoff) ----
    if cutoff < ENTRY_CUTOFF:
        long_open, short_open = _open_counts(conn)
        for sym, q in qual.items():
            if sym not in piv:
                continue
            side = q["side"]; basket = q["basket"]
            pv = piv[sym]; pp, r1, s1 = pv["pp"], pv["r1"], pv["s1"]
            tl = _two_closes_upto(conn, sym, d, cutoff)
            if not tl:
                continue
            prev_close, cur_close, cur_ts = tl
            if side == "LONG" and (prev_close <= pp < cur_close <= r1):
                if _has_open(conn, sym, "LONG"): continue
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
            elif side == "SHORT" and (prev_close >= pp > cur_close >= s1):
                if _has_open(conn, sym, "SHORT"): continue
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


# ── full replay ──────────────────────────────────────────────────────────────

def _step_times() -> List[time]:
    out = []
    cur = datetime(2000,1,1, DAY_START.hour, DAY_START.minute)
    end = datetime(2000,1,1, DAY_END.hour, DAY_END.minute)
    while cur <= end:
        out.append(cur.time())
        cur += timedelta(minutes=STEP_MINUTES)
    return out


def run_replay(conn, start: date, end: date = None, wipe: bool = True) -> dict:
    """
    Run the stepped 5-min replay from `start` to `end` (inclusive).
    wipe=True clears the paper book first (DESTRUCTIVE).
    """
    end = end or date.today()
    summary = {"start": str(start), "end": str(end), "wiped": None,
               "days": [], "totals": {}}

    if wipe:
        summary["wiped"] = wipe_book(conn)

    qual = qualified_proxy(conn)
    summary["qualified_universe"] = len(qual)

    days = _trading_days(conn, start, end)
    summary["trading_days"] = [str(d) for d in days]

    steps = _step_times()
    tot_entries = tot_exits = tot_gate = 0

    for d in days:
        build_pivots_for_day(conn, d)
        piv = _pivots_for(conn, d)
        buy_slots, sell_slots, fails = gate_slots_for_day(conn, d)
        d_entries = d_exits = d_gate = 0
        for i, cutoff in enumerate(steps):
            res = _replay_tick(conn, d, cutoff, qual, piv,
                               buy_slots, sell_slots, is_new_day=(i == 0))
            d_entries += len(res["entries"])
            d_exits   += len(res["exits"])
            d_gate    += len(res["gate_exits"])
        summary["days"].append({
            "date": str(d), "pivots": len(piv), "gate_fails": fails,
            "buy_slots": buy_slots, "sell_slots": sell_slots,
            "entries": d_entries, "exits": d_exits, "gate_exits": d_gate,
        })
        tot_entries += d_entries; tot_exits += d_exits; tot_gate += d_gate

    # final book + realized stats
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM v8_paper_positions WHERE status='OPEN'")
        open_n = cur.fetchone()[0]
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE pnl > 0),
                   COALESCE(ROUND(SUM(pnl)::numeric,2),0),
                   COALESCE(ROUND(AVG(return_pct)::numeric,2),0)
            FROM v8_paper_trades
        """)
        n, wins, total_pnl, avg_ret = cur.fetchone()

    summary["totals"] = {
        "entries": tot_entries, "exits": tot_exits, "gate_exits": tot_gate,
        "closed_trades": n, "wins": wins,
        "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
        "total_pnl": float(total_pnl), "avg_return_pct": float(avg_ret),
        "open_positions": open_n,
    }
    log.info(f"replay done: {summary['totals']}")
    return summary
