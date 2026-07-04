"""
SmartGain Daily M2M — Scorr (cc#166 + cc#179, SMARTGAIN_DAILY_M2M_SPEC_V1 = session_log 1393)
=============================================================================================
GET /api/smartgain/daily_m2m — M2M series computed ENTIRELY by FIFO replay of
smartgain_orders + smartgain_opening_positions (broker Net-Position checksum).
NO snapshot reads: never derives positions from smartgain_m2m/smartgain_holdings
(holdings used only as a reconciliation CHECKSUM in the response).

Two request shapes:
  * legacy (cc#166):  ?week_start=YYYY-MM-DD  -> Mon-Fri week bars (home card).
                       Response keys days[]/week_start/week_end/week_m2m/
                       closed_positions[] kept BYTE-COMPATIBLE for scorr_home.html.
  * ranged (cc#179):  ?range=1d|1w|1m|1y      -> price-chart-style series for the
                       /holdings M2M Chart V2 tab, + closed_all[] for the Closed
                       Positions tab. 1D = intraday 5-min curve vs fyers_fut closes.

Opening truth per week = smartgain_opening_positions (account, week_start, symbol,
direction, qty, avg_price). Orderbook history begins 2026-06-29 (OB_20260702_01);
1M/1Y render only the available range (from_date in the response) — never fabricate.

Daily M2M(d) = realised(d) + [unrealised(EOD d) - unrealised(EOD d-1)]. Day close =
last fyers_fut 5-min bar, fallback raw_prices close (NIFTY futures root -> NIFTY50
spot alias, cc#162). Recompute on demand every call — no state, no snapshots.
"""

import os
from collections import deque
from datetime import date, datetime, time as dt_time, timedelta
from typing import Optional

import psycopg
from fastapi import APIRouter, Query

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

DEFAULT_ACCOUNT = "MHK40"
SPOT_ALIAS = {"NIFTY": "NIFTY50"}          # cc#162 futures-root -> spot key
MKT_OPEN, MKT_CLOSE = dt_time(9, 15), dt_time(15, 30)


def _conn():
    return psycopg.connect(DATABASE_URL)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _ist_today() -> date:
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def _day_close(cur, symbol: str, d: date):
    """Last fyers_fut 5m bar of day d; fallback raw_prices spot close."""
    cur.execute("""
        SELECT close FROM intraday_prices
        WHERE symbol=%s AND timeframe='5m' AND source='fyers_fut' AND ts::date=%s
        ORDER BY ts DESC LIMIT 1
    """, (symbol, d))
    r = cur.fetchone()
    if r and r[0] is not None:
        return float(r[0]), "fyers_fut"
    spot = SPOT_ALIAS.get(symbol, symbol)
    cur.execute("SELECT close FROM raw_prices WHERE symbol=%s AND price_date=%s", (spot, d))
    r = cur.fetchone()
    if r and r[0] is not None:
        return float(r[0]), "raw_prices"
    return None, None


def _prev_trading_day(cur, before: date) -> Optional[date]:
    cur.execute("SELECT MAX(price_date) FROM raw_prices WHERE price_date < %s", (before,))
    r = cur.fetchone()
    return r[0] if r else None


def _trading_days(cur, start: date, end: date):
    cur.execute("""SELECT DISTINCT price_date FROM raw_prices
                   WHERE price_date BETWEEN %s AND %s ORDER BY price_date""", (start, end))
    return [r[0] for r in cur.fetchall()]


def _lot_unreal(lot, close: float) -> float:
    if lot["direction"] == "LONG":
        return (close - lot["price"]) * lot["qty"]
    return (lot["price"] - close) * lot["qty"]


def _apply_fill(books, closed, symbol, side, qty, price, trade_date):
    """FIFO per symbol: opposite-side fill closes oldest open lots first;
    leftover qty opens a new lot. Realized: LONG (exit-entry)*q, SHORT (entry-exit)*q."""
    book = books.setdefault(symbol, deque())
    fill_dir = "LONG" if side == "BUY" else "SHORT"
    remaining = qty
    while remaining > 0 and book and book[0]["direction"] != fill_dir:
        lot = book[0]
        matched = min(remaining, lot["qty"])
        if lot["direction"] == "LONG":
            pnl = (price - lot["price"]) * matched
        else:
            pnl = (lot["price"] - price) * matched
        closed.append({
            "symbol": symbol, "direction": lot["direction"], "qty": matched,
            "entry": round(lot["price"], 2), "exit": round(price, 2),
            "pnl": round(pnl, 2), "close_date": str(trade_date),
        })
        lot["qty"] -= matched
        remaining -= matched
        if lot["qty"] == 0:
            book.popleft()
    if remaining > 0:
        book.append({"direction": fill_dir, "qty": remaining, "price": float(price)})


def _load_inception(cur, account):
    cur.execute("""SELECT MIN(week_start) FROM smartgain_opening_positions WHERE account=%s""", (account,))
    r = cur.fetchone()
    inception = r[0] if r and r[0] else None
    opening = []
    if inception:
        cur.execute("""SELECT symbol, direction, qty, avg_price FROM smartgain_opening_positions
                       WHERE account=%s AND week_start=%s ORDER BY symbol""", (account, inception))
        opening = [{"symbol": r[0], "direction": r[1], "qty": int(r[2]), "avg_price": float(r[3])}
                   for r in cur.fetchall()]
    return inception, opening


def _fresh_books(opening):
    books = {}
    for op in opening:
        books.setdefault(op["symbol"], deque()).append(
            {"direction": op["direction"], "qty": op["qty"], "price": op["avg_price"]})
    return books


def _replay_full(cur, account, end_date):
    """Replay inception..end_date once. Returns per_day{date:(realised, unreal_eod)},
    trading days, full FIFO closed list (chronological), end books, opening,
    inception, and the inception baseline unrealised (opening at prev-tday close)."""
    inception, opening = _load_inception(cur, account)
    notes = []
    if not inception:
        return {}, [], [], {}, [], None, 0.0, ["no opening-position checksum stored — replay assumes flat open"]

    cur.execute("""SELECT trade_date, order_ts, symbol, side, qty, price FROM smartgain_orders
                   WHERE account=%s AND status='FILLED' AND trade_date <= %s
                   ORDER BY order_ts, id""", (account, end_date))
    fills = cur.fetchall()

    books = _fresh_books(opening)
    closed = []

    baseline_day = _prev_trading_day(cur, inception)
    inception_baseline = 0.0
    for op in opening:
        close = None
        if baseline_day:
            close, _s = _day_close(cur, op["symbol"], baseline_day)
        if close is None:
            close = op["avg_price"]
        inception_baseline += _lot_unreal(
            {"direction": op["direction"], "qty": op["qty"], "price": op["avg_price"]}, close)

    tdays = _trading_days(cur, inception, end_date)
    per_day, fi = {}, 0
    for d in tdays:
        nb = len(closed)
        while fi < len(fills) and fills[fi][0] <= d:
            _td, _ts, sym, side, qty, price = fills[fi]
            _apply_fill(books, closed, sym, side, int(qty), float(price), d)
            fi += 1
        realised_d = sum(c["pnl"] for c in closed[nb:])
        unreal_eod = 0.0
        for sym, book in books.items():
            if not book:
                continue
            close, _s = _day_close(cur, sym, d)
            for lot in book:
                unreal_eod += _lot_unreal(lot, close if close is not None else lot["price"])
        per_day[d] = (round(realised_d, 2), round(unreal_eod, 2))
    return per_day, tdays, closed, books, opening, inception, round(inception_baseline, 2), notes


def _closed_all_display(closed):
    """Full FIFO closes with running realised total; newest first for the Closed tab."""
    rt, out = 0.0, []
    for c in closed:
        rt += c["pnl"]
        out.append({**c, "running_total": round(rt, 2)})
    out.reverse()
    return out


def _open_positions(cur, books, as_of: date):
    out = []
    for sym in sorted(books):
        for lot in books[sym]:
            close, _s = _day_close(cur, sym, as_of)
            out.append({"symbol": sym, "direction": lot["direction"], "qty": lot["qty"],
                        "avg_entry": round(lot["price"], 2), "last_close": close,
                        "unrealised": round(_lot_unreal(lot, close), 2) if close is not None else None})
    return out


def _holdings_mismatch(cur, account, open_positions):
    cur.execute("""SELECT symbol, direction, qty FROM smartgain_holdings WHERE account=%s""", (account,))
    held = {(r[0], r[1]): int(r[2]) for r in cur.fetchall()}
    replay = {(p["symbol"], p["direction"]): p["qty"] for p in open_positions}
    mm = []
    for k in sorted(set(held) | set(replay)):
        if held.get(k, 0) != replay.get(k, 0):
            mm.append({"symbol": k[0], "direction": k[1],
                       "holdings_qty": held.get(k, 0), "replay_qty": replay.get(k, 0)})
    return mm


# ── cc#179: ranged endpoint ─────────────────────────────────────────────────

def _daily_range(cur, account, rng, today):
    per_day, tdays, closed, end_books, opening, inception, inception_baseline, notes = \
        _replay_full(cur, account, today)
    if not inception:
        return {"error": "no opening positions", "notes": notes}

    if rng == "1w":
        start, mode = _monday(today), "bar"
    elif rng == "1m":
        start, mode = today - timedelta(days=30), "bar"
    elif rng == "1y":
        start, mode = today - timedelta(days=365), "line"
    else:
        return {"error": f"bad range '{rng}'"}
    start = max(start, inception)

    window = [d for d in tdays if start <= d <= today]
    prior = [d for d in tdays if d < (window[0] if window else start)]
    prev_unreal = per_day[prior[-1]][1] if prior else inception_baseline

    series, cum = [], 0.0
    for d in window:
        realised, unreal = per_day[d]
        m2m = realised + (unreal - prev_unreal)
        cum += m2m
        series.append({"date": str(d), "label": d.strftime("%d %b"), "day": d.strftime("%a"),
                       "realised": round(realised, 2), "unrealised_eod": round(unreal, 2),
                       "m2m": round(m2m, 2), "cumulative": round(cum, 2)})
        prev_unreal = unreal

    open_pos = _open_positions(cur, end_books, today)
    return {
        "account": account, "range": rng, "mode": mode, "x_type": "date",
        "from_date": str(inception), "series": series,
        "total_m2m": round(sum(s["m2m"] for s in series), 2),
        "total_realised": round(sum(s["realised"] for s in series), 2),
        "closed_all": _closed_all_display(closed),
        "open_positions": open_pos,
        "holdings_mismatch": _holdings_mismatch(cur, account, open_pos),
        "notes": notes, "computed_at": datetime.utcnow().isoformat() + "Z",
    }


def _load_fut_5m(cur, symbols, d):
    """{symbol: [(time, close), ...]} sorted, day d, fyers_fut 5m. For intraday ffill."""
    if not symbols:
        return {}
    cur.execute("""SELECT symbol, ts::time, close FROM intraday_prices
                   WHERE symbol = ANY(%s) AND timeframe='5m' AND source='fyers_fut' AND ts::date=%s
                   ORDER BY symbol, ts""", (list(symbols), d))
    out = {}
    for sym, t, close in cur.fetchall():
        if close is not None:
            out.setdefault(sym, []).append((t, float(close)))
    return out


def _ffill_price(series, t):
    """Last close at or before time t from a sorted [(time, close)] list; None if none yet."""
    val = None
    for bt, c in series:
        if bt <= t:
            val = c
        else:
            break
    return val


def _intraday_1d(cur, account, today):
    """1D intraday M2M curve, 5-min resolution, replayed against fyers_fut closes.
    Target day = latest day that has fyers_fut 5m bars (today when live, else last)."""
    cur.execute("""SELECT MAX(ts::date) FROM intraday_prices
                   WHERE timeframe='5m' AND source='fyers_fut' AND ts::date <= %s""", (today,))
    r = cur.fetchone()
    target = r[0] if r and r[0] else today

    inception, opening = _load_inception(cur, account)
    if not inception:
        return {"error": "no opening positions"}

    # book at start of target day = replay every fill strictly before target
    cur.execute("""SELECT trade_date, order_ts, symbol, side, qty, price FROM smartgain_orders
                   WHERE account=%s AND status='FILLED' AND trade_date < %s
                   ORDER BY order_ts, id""", (account, target))
    books = _fresh_books(opening)
    presink = []
    for _td, _ts, sym, side, qty, price in cur.fetchall():
        _apply_fill(books, presink, sym, side, int(qty), float(price), _td)

    # baseline: start-of-day book valued at prev trading day close
    prev_day = _prev_trading_day(cur, target)
    baseline = 0.0
    for sym, book in books.items():
        if not book:
            continue
        close = None
        if prev_day:
            close, _s = _day_close(cur, sym, prev_day)
        for lot in book:
            baseline += _lot_unreal(lot, close if close is not None else lot["price"])

    # today's fills, chronological with intraday time
    cur.execute("""SELECT order_ts, symbol, side, qty, price FROM smartgain_orders
                   WHERE account=%s AND status='FILLED' AND trade_date=%s
                   ORDER BY order_ts, id""", (account, target))
    today_fills = [(ts.time() if hasattr(ts, "time") else ts, sym, side, int(qty), float(price))
                   for ts, sym, side, qty, price in cur.fetchall()]

    syms = set(books) | {f[1] for f in today_fills}
    bars = _load_fut_5m(cur, syms, target)

    # 5-min grid 09:15..15:30
    grid, t = [], datetime.combine(target, MKT_OPEN)
    end = datetime.combine(target, MKT_CLOSE)
    while t <= end:
        grid.append(t.time())
        t += timedelta(minutes=5)

    realised_run, closed_run, fi = 0.0, [], 0
    partial_syms = set()
    series = []
    for gt in grid:
        while fi < len(today_fills) and today_fills[fi][0] <= gt:
            _t, sym, side, qty, price = today_fills[fi]
            nb = len(closed_run)
            _apply_fill(books, closed_run, sym, side, qty, price, target)
            realised_run += sum(c["pnl"] for c in closed_run[nb:])
            fi += 1
        unreal = 0.0
        for sym, book in books.items():
            if not book:
                continue
            px = _ffill_price(bars.get(sym, []), gt)
            if px is None:
                px, _s = _day_close(cur, SPOT_ALIAS.get(sym, sym), prev_day) if prev_day else (None, None)
            if px is None:
                partial_syms.add(sym)
                px = book[0]["price"]     # carry at entry -> 0 contribution
            for lot in book:
                unreal += _lot_unreal(lot, px)
        series.append({"label": gt.strftime("%H:%M"),
                       "m2m": round(realised_run + (unreal - baseline), 2),
                       "realised": round(realised_run, 2),
                       "unrealised": round(unreal, 2)})

    notes = []
    if partial_syms:
        notes.append(f"intraday feed partial for {', '.join(sorted(partial_syms))} — "
                     f"pre-feed buckets carried at entry")
    return {
        "account": account, "range": "1d", "mode": "line", "x_type": "time",
        "target_day": str(target), "from_date": str(inception),
        "series": series,
        "total_m2m": series[-1]["m2m"] if series else 0.0,
        "notes": notes, "computed_at": datetime.utcnow().isoformat() + "Z",
    }


def _week_response(week_start, account):
    """Legacy cc#166 path — BYTE-COMPATIBLE keys for scorr_home.html. Unchanged."""
    today = _ist_today()
    try:
        ws = _monday(datetime.strptime(week_start, "%Y-%m-%d").date()) if week_start else _monday(today)
    except ValueError:
        return {"error": f"bad week_start '{week_start}', expected YYYY-MM-DD"}
    week_days = [ws + timedelta(days=i) for i in range(5)]
    notes = []
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, direction, qty, avg_price FROM smartgain_opening_positions
                       WHERE account=%s AND week_start=%s ORDER BY symbol""", (account, ws))
        opening = [{"symbol": r[0], "direction": r[1], "qty": int(r[2]), "avg_price": float(r[3])}
                   for r in cur.fetchall()]
        if not opening:
            notes.append("no opening-position checksum stored for this week — replay assumes flat open")
        cur.execute("""SELECT trade_date, order_ts, symbol, side, qty, price FROM smartgain_orders
                       WHERE account=%s AND status='FILLED' AND trade_date BETWEEN %s AND %s
                       ORDER BY order_ts, id""", (account, ws, week_days[-1]))
        fills = cur.fetchall()
        books = _fresh_books(opening)
        closed = []
        baseline_day = _prev_trading_day(cur, ws)
        prev_unreal = 0.0
        for op in opening:
            close = None
            if baseline_day:
                close, _s = _day_close(cur, op["symbol"], baseline_day)
            if close is None:
                notes.append(f"baseline close missing for {op['symbol']} — Monday absorbs its carried move")
                close = op["avg_price"]
            prev_unreal += _lot_unreal({"direction": op["direction"], "qty": op["qty"],
                                        "price": op["avg_price"]}, close)
        days_out, fill_idx, cumulative = [], 0, 0.0
        for d in week_days:
            if d > today:
                break
            nb = len(closed)
            while fill_idx < len(fills) and fills[fill_idx][0] == d:
                _td, _ts, sym, side, qty, price = fills[fill_idx]
                _apply_fill(books, closed, sym, side, int(qty), float(price), d)
                fill_idx += 1
            realised_d = sum(c["pnl"] for c in closed[nb:])
            unreal_d = 0.0
            for sym, book in books.items():
                if not book:
                    continue
                close, _s = _day_close(cur, sym, d)
                if close is None:
                    notes.append(f"no close for {sym} on {d} — its unrealised carried at entry (0)")
                for lot in book:
                    unreal_d += _lot_unreal(lot, close if close is not None else lot["price"])
            m2m = realised_d + (unreal_d - prev_unreal)
            cumulative += m2m
            days_out.append({"date": str(d), "day": d.strftime("%a"), "realised": round(realised_d, 2),
                             "unrealised_eod": round(unreal_d, 2), "m2m": round(m2m, 2),
                             "cumulative": round(cumulative, 2)})
            prev_unreal = unreal_d
        open_out = _open_positions(cur, books, min(today, week_days[-1]))
        mismatch = _holdings_mismatch(cur, account, open_out)
        if mismatch:
            notes.append("replay/holdings mismatch — an orderbook batch is likely missing from smartgain_orders")
    return {
        "account": account, "week_start": str(ws), "week_end": str(week_days[-1]),
        "opening_positions": opening, "days": days_out,
        "week_m2m": round(sum(x["m2m"] for x in days_out), 2),
        "week_realised": round(sum(x["realised"] for x in days_out), 2),
        "closed_positions": closed, "open_positions": open_out,
        "holdings_mismatch": mismatch, "notes": notes,
        "computed_at": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/api/smartgain/daily_m2m")
def smartgain_daily_m2m(week_start: Optional[str] = None,
                        range: Optional[str] = Query(None),
                        account: str = DEFAULT_ACCOUNT):
    # cc#179: range param -> price-chart-style series (/holdings). No range ->
    # legacy cc#166 week shape (scorr_home.html), untouched.
    if range:
        rng = range.lower()
        with _conn() as conn, conn.cursor() as cur:
            if rng == "1d":
                return _intraday_1d(cur, account, _ist_today())
            if rng in ("1w", "1m", "1y"):
                return _daily_range(cur, account, rng, _ist_today())
        return {"error": f"bad range '{range}', expected 1d|1w|1m|1y"}
    return _week_response(week_start, account)
