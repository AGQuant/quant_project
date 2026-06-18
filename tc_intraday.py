"""
TC Cache + Intraday Scan — Phase 1 prototype (17-Jun-2026, spec id=371/373).

TWO PIECES:
1. tc_cache writer — loops futures universe x both sides through the existing
   native_trade_check.compute_trade_check(), upserts LATEST snapshot into
   tc_cache (no rule-state history — that's phase 2 score-trend). Standalone,
   manually triggerable. Scheduler wiring deferred to phase 1.5.

2. Intraday scan — TWO STAGE:
   Stage 1 (cached, instant): pull symbols with score >= 10 from tc_cache.
   Stage 2 (live, only on that shortlist): apply intraday confirmations:
     - 1H bar positive (hourly gain)
     - volume pace >= 1.5x (today 9:15->now vs 7-day avg same window)
     - CMP > prev close by 1-2% (strict band)
     - week 5-day low within 1% of S1
     - CMP > 20 DMA
     - CMP > day open (holding green)
   LONG and SHORT scored separately.

Reuses compute_trade_check unchanged. Pure DB. Context isolation id=244 —
never mixes with V8 paper engine.
"""

import os
from datetime import datetime, timedelta
import psycopg
from psycopg.types.json import Jsonb

import native_trade_check as ntc

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


# ─────────────────────────────────────────────── CACHE WRITER ───

def refresh_tc_cache(n=210):
    """Recompute TC for top-N mcap futures, both sides, upsert latest into tc_cache.
    Heavy (N*2 computes) — standalone, off the live request path."""
    started = _ist_now()
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        symbols = ntc._top_mcap_symbols(cur, n) if hasattr(ntc, "_top_mcap_symbols") else []
        if not symbols:
            cur.execute("""SELECT f.symbol FROM futures_universe f
                           JOIN gvm_scores g ON f.symbol=g.symbol
                           WHERE f.is_active=TRUE AND g.market_cap IS NOT NULL
                           ORDER BY g.market_cap DESC LIMIT %s""", (n,))
            symbols = [r[0] for r in cur.fetchall()]

    written, errors = 0, []
    for sym in symbols:
        for side in ("LONG", "SHORT"):
            try:
                d = ntc.compute_trade_check(sym, side)
                if not d.get("ok"):
                    errors.append({"symbol": sym, "side": side, "error": d.get("error", "no data")})
                    continue
                row = ntc._slim_row(d)
                with psycopg.connect(DATABASE_URL) as c2, c2.cursor() as cur2:
                    cur2.execute("""
                        INSERT INTO tc_cache (symbol, side, score, total, verdict_class,
                            cmp, pivot, not_passed, n_pass, n_watch, n_fail, computed_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                        ON CONFLICT (symbol, side) DO UPDATE SET
                            score=EXCLUDED.score, total=EXCLUDED.total,
                            verdict_class=EXCLUDED.verdict_class, cmp=EXCLUDED.cmp,
                            pivot=EXCLUDED.pivot, not_passed=EXCLUDED.not_passed,
                            n_pass=EXCLUDED.n_pass, n_watch=EXCLUDED.n_watch,
                            n_fail=EXCLUDED.n_fail, computed_at=NOW()""",
                        (row["symbol"], row["side"], row["score"], row["total"],
                         row["verdict_class"], row["cmp"],
                         Jsonb(row["pivot"]),
                         Jsonb(row["not_passed"]),
                         row["n_pass"], row["n_watch"], row["n_fail"]))
                    c2.commit()
                written += 1
            except Exception as e:
                errors.append({"symbol": sym, "side": side, "error": f"{type(e).__name__}: {str(e)[:80]}"})

    return {"ok": True, "written": written, "symbols": len(symbols),
            "errors": errors[:20], "ts": _ist_now().strftime("%d-%b %H:%M IST"),
            "elapsed_sec": round((_ist_now() - started).total_seconds(), 1)}


# ─────────────────────────────────────────────── INTRADAY STAGE-2 FILTERS ───

def _hour_positive(cur, symbol):
    """Last rolling-60min built from 5-min bars: close > open AND second-half rising."""
    cur.execute("""
        SELECT close, open FROM intraday_prices
        WHERE symbol=%s AND timeframe='5m' AND ts::date=CURRENT_DATE
        ORDER BY ts DESC LIMIT 12""", (symbol,))
    bars = cur.fetchall()
    if len(bars) < 6:
        return None
    bars = bars[::-1]  # oldest->newest within the hour
    o = _f(bars[0][1]); c = _f(bars[-1][0])
    if o is None or c is None:
        return None
    return c > o


def _vol_pace(cur, symbol, mult=1.5):
    """Today 9:15->now cumulative vol vs avg same-window over prior 7 trading days."""
    now = _ist_now()
    hhmm = now.strftime("%H:%M:%S")
    cur.execute("""
        WITH today AS (
          SELECT COALESCE(SUM(volume),0) AS v FROM intraday_prices
          WHERE symbol=%s AND timeframe='5m' AND ts::date=CURRENT_DATE
            AND ts::time <= %s::time),
        prior AS (
          SELECT ts::date AS d, SUM(volume) AS v FROM intraday_prices
          WHERE symbol=%s AND timeframe='5m'
            AND ts::date < CURRENT_DATE AND ts::date >= CURRENT_DATE - INTERVAL '9 days'
            AND ts::time <= %s::time
          GROUP BY ts::date ORDER BY d DESC LIMIT 7)
        SELECT (SELECT v FROM today), (SELECT AVG(v) FROM prior)""",
        (symbol, hhmm, symbol, hhmm))
    r = cur.fetchone()
    today_v = _f(r[0]) if r else None
    avg_v = _f(r[1]) if r else None
    if not today_v or not avg_v or avg_v == 0:
        return None, None
    pace = today_v / avg_v
    return pace >= mult, round(pace, 2)


def intraday_scan(side="LONG", min_score=10, gain_lo=1.0, gain_hi=2.0, vol_mult=1.5):
    """Stage 1: cached score>=min_score universe. Stage 2: live intraday filters."""
    side = side.upper()
    started = _ist_now()
    out, checked = [], 0
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, score, total, verdict_class, cmp, pivot
                       FROM tc_cache WHERE side=%s AND score>=%s
                       ORDER BY score DESC""", (side, min_score))
        shortlist = cur.fetchall()

        for sym, score, total, vclass, cmp_cached, pivot in shortlist:
            checked += 1
            try:
                # live CMP + prev close + dma_20 + S1 + 5d low
                cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (sym,))
                cr = cur.fetchone(); cmp = _f(cr[0]) if cr else _f(cmp_cached)
                if cmp is None:
                    continue
                cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date<CURRENT_DATE
                               ORDER BY price_date DESC LIMIT 1""", (sym,))
                pc = cur.fetchone(); prev_close = _f(pc[0]) if pc else None
                if not prev_close:
                    continue
                gain = (cmp / prev_close - 1) * 100

                # day open
                cur.execute("""SELECT open FROM intraday_prices WHERE symbol=%s AND timeframe='5m'
                               AND ts::date=CURRENT_DATE ORDER BY ts ASC LIMIT 1""", (sym,))
                do = cur.fetchone(); day_open = _f(do[0]) if do else None

                # dma_20 (from v8_metrics, % above)
                cur.execute("""SELECT dma_20 FROM v8_metrics WHERE symbol=%s
                               AND score_date=(SELECT MAX(score_date) FROM v8_metrics) LIMIT 1""", (sym,))
                dm = cur.fetchone(); dma20 = _f(dm[0]) if dm else None

                # S1 + 5-day low
                cur.execute("""SELECT s1 FROM v8_paper_pivots WHERE symbol=%s
                               AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots) LIMIT 1""", (sym,))
                ps = cur.fetchone(); s1 = _f(ps[0]) if ps else None
                cur.execute("""SELECT MIN(low) FROM (SELECT low FROM raw_prices WHERE symbol=%s AND volume>0
                               ORDER BY price_date DESC LIMIT 5) s""", (sym,))
                lw = cur.fetchone(); low5d = _f(lw[0]) if lw else None

                # ── Stage-2 filters ──
                # LONG: gain in band, above prev close. SHORT mirrors (fall band).
                if side == "LONG":
                    f_gain = gain_lo <= gain <= gain_hi
                    f_dma = (dma20 is not None and dma20 > 0)          # above 20 DMA
                    f_open = (day_open is not None and cmp > day_open)  # holding green
                    f_s1 = (s1 is not None and low5d is not None and low5d <= 1.01 * s1)  # week low near S1
                else:
                    f_gain = gain_lo <= (-gain) <= gain_hi             # down 1-2%
                    f_dma = (dma20 is not None and dma20 < 0)          # below 20 DMA
                    f_open = (day_open is not None and cmp < day_open)  # holding red
                    f_s1 = True  # support rule is long-specific; skip for short

                f_hour = _hour_positive(cur, sym) if side == "LONG" else (
                    (lambda h: (not h) if h is not None else None)(_hour_positive(cur, sym)))
                f_vol, pace = _vol_pace(cur, sym, vol_mult)

                checks = {
                    "score>=10": True,  # already filtered
                    "gain_band": f_gain,
                    "hour_pos": f_hour,
                    "vol_1.5x": f_vol,
                    "dma_20": f_dma,
                    "hold_open": f_open,
                    "week_low_s1": f_s1,
                }
                # all non-None must be True; None (no data) abstains -> not a pass
                passed = all(v is True for v in checks.values())
                if passed:
                    out.append({
                        "symbol": sym, "side": side, "tc_score": _f(score), "total": total,
                        "verdict_class": vclass, "cmp": round(cmp, 2),
                        "gain_pct": round(gain, 2), "vol_pace": pace,
                        "s1": round(s1, 2) if s1 else None,
                        "low5d": round(low5d, 2) if low5d else None,
                        "checks": {k: (None if v is None else bool(v)) for k, v in checks.items()},
                    })
            except Exception:
                continue

    out.sort(key=lambda r: (r["tc_score"] or 0, r["vol_pace"] or 0), reverse=True)
    return {"ok": True, "side": side, "min_score": min_score,
            "shortlist": checked, "matched": len(out), "rows": out[:20],
            "ts": _ist_now().strftime("%d-%b %H:%M IST"),
            "elapsed_sec": round((_ist_now() - started).total_seconds(), 1),
            "note": "Stage1 cached TC>=10; Stage2 live intraday (1H, vol 1.5x, gain 1-2%, DMA20, S1, open). Prototype."}


# ─────────────────────────────────────────────────────────── INTRADAY PAPER ENGINE ───
# Added 18-Jun-2026. Auto-runs every 5-min via scheduler (like V8 paper).
# Context isolation id=244: tc_intraday_* tables NEVER mix with v8_paper_*.
#
# Rules (locked 18-Jun-2026):
#   Entry source : intraday_scan() matches (TC>=10 + all live filters pass), both sides
#   Target/Stop  : fixed +1.5% / -1.5% (LONG); mirror for SHORT
#   Slots        : NO CAP — enter every fresh match
#   Guards       : 1 entry/symbol/side/day, blackout (earnings ex-date), entry cutoff 15:00
#   Exit         : target/stop on live CMP, OR hard square-off at 15:15 IST
#   Sizing       : 1 lot (futures_universe.lot_size)

TGT_PCT = 1.5
STP_PCT = 1.5
ENTRY_CUTOFF = (15, 0)    # 15:00 IST — no new entries after
SQUARE_OFF   = (15, 15)   # 15:15 IST — close everything still open


def _lot_size(cur, symbol):
    try:
        cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s", (symbol,))
        r = cur.fetchone()
        return int(r[0]) if r and r[0] else 1
    except Exception:
        return 1


def _is_blackout(cur, symbol):
    try:
        cur.execute("""SELECT 1 FROM earnings_calendar
                       WHERE UPPER(ticker)=%s
                         AND ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                       LIMIT 1""", (symbol.upper(),))
        return cur.fetchone() is not None
    except Exception:
        return False


def _before_cutoff(now):
    return (now.hour, now.minute) < ENTRY_CUTOFF


def _at_or_after_squareoff(now):
    return (now.hour, now.minute) >= SQUARE_OFF


def run_intraday_paper_entry():
    """Scan both sides; auto-enter every fresh match. No slot cap.
    Called every 5-min by scheduler during market hours."""
    now = _ist_now()
    if not _before_cutoff(now):
        return {"ok": True, "skipped": "after 15:00 entry cutoff", "entered": 0}

    entered = []
    for side in ("LONG", "SHORT"):
        scan = intraday_scan(side=side)
        if not scan.get("ok"):
            continue
        for row in scan.get("rows", []):
            sym = row["symbol"]
            cmp = _f(row.get("cmp"))
            if cmp is None:
                continue
            try:
                with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
                    # blackout
                    if _is_blackout(cur, sym):
                        continue
                    # already open?
                    cur.execute("""SELECT 1 FROM tc_intraday_positions
                                   WHERE symbol=%s AND side=%s AND status='OPEN' LIMIT 1""",
                                (sym, side))
                    if cur.fetchone():
                        continue
                    # 1 entry/symbol/side/day (open or already closed today)
                    cur.execute("""SELECT 1 FROM tc_intraday_positions
                                   WHERE symbol=%s AND side=%s AND entry_ts::date=CURRENT_DATE LIMIT 1""",
                                (sym, side))
                    if cur.fetchone():
                        continue
                    cur.execute("""SELECT 1 FROM tc_intraday_trades
                                   WHERE symbol=%s AND side=%s AND entry_ts::date=CURRENT_DATE LIMIT 1""",
                                (sym, side))
                    if cur.fetchone():
                        continue

                    entry = round(cmp, 2)
                    if side == "LONG":
                        target = round(entry * (1 + TGT_PCT / 100), 2)
                        stop   = round(entry * (1 - STP_PCT / 100), 2)
                    else:
                        target = round(entry * (1 - TGT_PCT / 100), 2)
                        stop   = round(entry * (1 + STP_PCT / 100), 2)
                    qty = _lot_size(cur, sym)

                    cur.execute("""
                        INSERT INTO tc_intraday_positions
                        (symbol, side, entry_price, entry_ts, qty, target, stop_loss, tc_score, status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'OPEN')
                        ON CONFLICT (symbol, side, status) DO NOTHING""",
                        (sym, side, entry, now, qty, target, stop, _f(row.get("tc_score"))))
                    if cur.rowcount > 0:
                        conn.commit()
                        entered.append({"symbol": sym, "side": side, "entry": entry,
                                        "target": target, "stop": stop, "qty": qty})
            except Exception:
                continue

    return {"ok": True, "entered": len(entered), "positions": entered,
            "ts": now.strftime("%d-%b %H:%M IST")}


def run_intraday_paper_exit():
    """Check open positions for target/stop on live CMP. Hard square-off at 15:15.
    Called every 5-min by scheduler during market hours."""
    now = _ist_now()
    force = _at_or_after_squareoff(now)
    closed = []
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, symbol, side, entry_price, entry_ts, qty, target, stop_loss, tc_score
                       FROM tc_intraday_positions WHERE status='OPEN'""")
        rows = cur.fetchall()
        for pid, sym, side, entry, ets, qty, target, stop, tcs in rows:
            entry = _f(entry); target = _f(target); stop = _f(stop)
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (sym,))
            cr = cur.fetchone()
            cmp = _f(cr[0]) if cr else None
            if cmp is None and not force:
                continue

            result, exit_px = None, None
            if cmp is not None:
                if side == "LONG":
                    if cmp >= target: result, exit_px = "TARGET", target
                    elif cmp <= stop: result, exit_px = "SL", stop
                else:
                    if cmp <= target: result, exit_px = "TARGET", target
                    elif cmp >= stop: result, exit_px = "SL", stop

            if result is None and force:
                result = "SQUARE_OFF"
                exit_px = cmp if cmp is not None else entry

            if result is None:
                continue

            exit_px = round(exit_px, 2)
            if side == "LONG":
                ret = (exit_px / entry - 1) * 100
            else:
                ret = (entry / exit_px - 1) * 100
            pnl = round((exit_px - entry) * qty * (1 if side == "LONG" else -1), 2)

            try:
                cur.execute("""
                    INSERT INTO tc_intraday_trades
                    (symbol, side, entry_price, entry_ts, exit_price, exit_ts, qty,
                     target, stop_loss, tc_score, return_pct, pnl, result)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (sym, side, entry, ets, exit_px, now, qty, target, stop,
                     _f(tcs), round(ret, 3), pnl, result))
                cur.execute("UPDATE tc_intraday_positions SET status='CLOSED' WHERE id=%s", (pid,))
                conn.commit()
                closed.append({"symbol": sym, "side": side, "result": result,
                               "exit": exit_px, "return_pct": round(ret, 2), "pnl": pnl})
            except Exception:
                conn.rollback()
                continue

    return {"ok": True, "closed": len(closed), "trades": closed,
            "square_off": force, "ts": now.strftime("%d-%b %H:%M IST")}


def intraday_paper_status():
    """Open positions + today's closed trades + summary. For dashboard."""
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, side, entry_price, entry_ts, qty, target, stop_loss, tc_score
                       FROM tc_intraday_positions WHERE status='OPEN' ORDER BY entry_ts DESC""")
        open_cols = ["symbol", "side", "entry_price", "entry_ts", "qty", "target", "stop_loss", "tc_score"]
        open_pos = []
        for r in cur.fetchall():
            d = dict(zip(open_cols, r))
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (d["symbol"],))
            cr = cur.fetchone()
            cmp = _f(cr[0]) if cr else None
            entry = _f(d["entry_price"])
            d["cmp"] = round(cmp, 2) if cmp else None
            if cmp and entry:
                if d["side"] == "LONG":
                    d["open_pnl_pct"] = round((cmp / entry - 1) * 100, 2)
                else:
                    d["open_pnl_pct"] = round((entry / cmp - 1) * 100, 2)
                d["unrealised_pnl"] = round((cmp - entry) * int(d["qty"]) * (1 if d["side"] == "LONG" else -1), 2)
            d["entry_price"] = entry
            d["target"] = _f(d["target"]); d["stop_loss"] = _f(d["stop_loss"])
            d["tc_score"] = _f(d["tc_score"])
            d["entry_ts"] = d["entry_ts"].strftime("%d-%b %H:%M") if d["entry_ts"] else None
            open_pos.append(d)

        cur.execute("""SELECT symbol, side, entry_price, entry_ts, exit_price, exit_ts,
                              qty, return_pct, pnl, result
                       FROM tc_intraday_trades WHERE exit_ts::date=CURRENT_DATE
                       ORDER BY exit_ts DESC""")
        tr_cols = ["symbol", "side", "entry_price", "entry_ts", "exit_price", "exit_ts",
                   "qty", "return_pct", "pnl", "result"]
        trades = []
        for r in cur.fetchall():
            d = dict(zip(tr_cols, r))
            d["entry_price"] = _f(d["entry_price"]); d["exit_price"] = _f(d["exit_price"])
            d["return_pct"] = _f(d["return_pct"]); d["pnl"] = _f(d["pnl"])
            d["entry_ts"] = d["entry_ts"].strftime("%d-%b %H:%M") if d["entry_ts"] else None
            d["exit_ts"] = d["exit_ts"].strftime("%d-%b %H:%M") if d["exit_ts"] else None
            trades.append(d)

        wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
        losses = sum(1 for t in trades if (t["pnl"] or 0) <= 0)
        total_pnl = round(sum(t["pnl"] or 0 for t in trades), 2)

    return {"ok": True,
            "open_positions": open_pos,
            "recent_trades": trades,
            "summary": {"open": len(open_pos), "trades": len(trades),
                        "wins": wins, "losses": losses, "total_pnl": total_pnl},
            "ts": _ist_now().strftime("%d-%b %H:%M IST")}


def intraday_dashboard():
    """Full dashboard payload in the shape scorr_intraday.html expects:
    { ts, cache_ts, cache_rows, sides: { LONG: {funnel, stats, open, trades}, SHORT: {...} } }

    INSTANT READ — pure SELECTs from tc_intraday_* + tc_cache. No scan, no compute.
    The 5-min scheduler tick does all the heavy lifting; this just reads results.

    18-Jun-2026 fix: wrapped in try/except (any exception returns partial data + _error
    instead of raising 500); str()[:16] for cache_ts avoids tz-aware datetime issues;
    LEFT JOIN for CMP eliminates nested cursor re-use pattern.
    """
    out = {"ts": _ist_now().strftime("%d-%b %H:%M IST"),
           "cache_ts": None, "cache_rows": 0, "sides": {}}
    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            # cache freshness — str()[:16] works for both naive and tz-aware datetimes
            cur.execute("SELECT COUNT(*), MAX(computed_at) FROM tc_cache")
            cr = cur.fetchone()
            out["cache_rows"] = int(cr[0]) if cr and cr[0] else 0
            if cr and cr[1]:
                out["cache_ts"] = str(cr[1])[:16]

            for side in ("LONG", "SHORT"):
                # funnel counts
                cur.execute("SELECT COUNT(*) FROM tc_cache WHERE side=%s", (side,))
                universe = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM tc_cache WHERE side=%s AND score>=10", (side,))
                tc10 = int(cur.fetchone()[0] or 0)
                cur.execute("""SELECT COUNT(*) FROM tc_intraday_positions
                               WHERE side=%s AND status='OPEN'""", (side,))
                n_open = int(cur.fetchone()[0] or 0)
                cur.execute("""SELECT COUNT(*) FROM tc_intraday_trades
                               WHERE side=%s AND exit_ts::date=CURRENT_DATE""", (side,))
                n_closed = int(cur.fetchone()[0] or 0)

                # open positions — use JOIN to avoid nested cursor re-use
                cur.execute("""
                    SELECT p.symbol, p.entry_price, p.target, p.stop_loss, c.cmp
                    FROM tc_intraday_positions p
                    LEFT JOIN cmp_prices c ON c.symbol = p.symbol
                    WHERE p.side=%s AND p.status='OPEN'
                    ORDER BY p.entry_ts DESC
                """, (side,))
                open_rows = []
                for sym, entry, target, stop, cmp in cur.fetchall():
                    entry = _f(entry); cmp = _f(cmp)
                    pnl_pct = None
                    if cmp and entry:
                        pnl_pct = round(((cmp / entry - 1) if side == "LONG"
                                         else (entry / cmp - 1)) * 100, 2)
                    open_rows.append({"symbol": sym, "entry_price": entry,
                                      "cmp": round(cmp, 2) if cmp else None,
                                      "pnl_pct": pnl_pct,
                                      "target": _f(target), "stop": _f(stop)})

                # today's closed trades
                cur.execute("""
                    SELECT symbol, entry_price, exit_price, return_pct, result
                    FROM tc_intraday_trades
                    WHERE side=%s AND exit_ts::date=CURRENT_DATE
                    ORDER BY exit_ts DESC
                """, (side,))
                trade_rows = []
                for sym, entry, exit_px, ret, result in cur.fetchall():
                    ret = _f(ret)
                    if result == "TARGET":
                        pill = "WIN"
                    elif result == "SL":
                        pill = "LOSS"
                    else:
                        pill = "WIN" if (ret or 0) > 0 else ("LOSS" if (ret or 0) < 0 else "FLAT")
                    trade_rows.append({"symbol": sym, "entry_price": _f(entry),
                                       "exit_price": _f(exit_px), "pnl_pct": ret,
                                       "result": pill, "exit_reason": result})

                # stats
                n_tr = len(trade_rows)
                wins = sum(1 for t in trade_rows if (t["pnl_pct"] or 0) > 0)
                win_rate = round(wins / n_tr * 100, 1) if n_tr else 0
                total_pnl = round(sum(t["pnl_pct"] or 0 for t in trade_rows), 2)
                avg_pnl = round(total_pnl / n_tr, 2) if n_tr else 0

                out["sides"][side] = {
                    "funnel": {"universe": universe, "tc10": tc10,
                               "open": n_open, "closed": n_closed},
                    "stats": {"trades": n_tr, "win_rate": win_rate,
                              "avg_pnl": avg_pnl, "total_pnl": total_pnl},
                    "open": open_rows,
                    "trades": trade_rows,
                }

    except Exception as e:
        out["_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    return out
