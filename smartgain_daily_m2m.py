"""
SmartGain Daily M2M — Scorr (cc#166, SMARTGAIN_DAILY_M2M_SPEC_V1 = session_log 1393)
=====================================================================================
GET /api/smartgain/daily_m2m — daily M2M series for the current (or given) week,
computed ENTIRELY by FIFO replay of smartgain_orders. NO snapshot reads: never
derives positions from smartgain_m2m/smartgain_holdings — holdings are used only
as an optional reconciliation CHECKSUM in the response (mismatch = missing
orderbook batch, per spec "new orderbook batch upload = graph auto-corrects").

Opening state per week = broker Net Position checksum stored per batch in
smartgain_opening_positions (account, week_start, symbol, direction, qty,
avg_price) — created by this task; week 2026-06-29 seeded from the
journal-reconciled openings (verified: their realized P&L reproduces the
addendum's broker-verified +2719.25 to the paisa, session_log 1272).

Day close prices: last fyers_fut 5-min bar of the day from intraday_prices,
fallback raw_prices close (NIFTY futures root -> NIFTY50 spot alias, cc#162).

Daily M2M(d) = realised(d) + [unrealised(end of d) - unrealised(end of d-1)].
Baseline (Monday's d-1) = opening lots valued at the last trading day before
week_start; if that close is unavailable, the lot's entry price is used
(baseline unrealised 0 -> Monday absorbs the carried move) and flagged.

Recompute on demand: every call replays from scratch — no state, no snapshots.
"""

import os
from collections import deque
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg
from fastapi import APIRouter

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

DEFAULT_ACCOUNT = "MHK40"
# cc#162: NIFTY futures-contract root symbol != the spot key used in raw_prices.
SPOT_ALIAS = {"NIFTY": "NIFTY50"}


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


@router.get("/api/smartgain/daily_m2m")
def smartgain_daily_m2m(week_start: Optional[str] = None, account: str = DEFAULT_ACCOUNT):
    today = _ist_today()
    try:
        ws = _monday(datetime.strptime(week_start, "%Y-%m-%d").date()) if week_start else _monday(today)
    except ValueError:
        return {"error": f"bad week_start '{week_start}', expected YYYY-MM-DD"}
    week_days = [ws + timedelta(days=i) for i in range(5)]           # Mon..Fri
    notes = []

    with _conn() as conn, conn.cursor() as cur:
        # 1) opening state — broker Net Position checksum for this week
        cur.execute("""
            SELECT symbol, direction, qty, avg_price FROM smartgain_opening_positions
            WHERE account=%s AND week_start=%s ORDER BY symbol
        """, (account, ws))
        opening = [{"symbol": r[0], "direction": r[1], "qty": int(r[2]), "avg_price": float(r[3])}
                   for r in cur.fetchall()]
        if not opening:
            notes.append("no opening-position checksum stored for this week — replay assumes flat open")

        # 2) fills for the week, chronological
        cur.execute("""
            SELECT trade_date, order_ts, symbol, side, qty, price FROM smartgain_orders
            WHERE account=%s AND status='FILLED' AND trade_date BETWEEN %s AND %s
            ORDER BY order_ts, id
        """, (account, ws, week_days[-1]))
        fills = cur.fetchall()

        # 3) FIFO replay day by day
        books, closed = {}, []
        for op in opening:
            books.setdefault(op["symbol"], deque()).append(
                {"direction": op["direction"], "qty": op["qty"], "price": op["avg_price"]})

        # baseline unrealised: opening lots at last trading day before week_start
        baseline_day = _prev_trading_day(cur, ws)
        prev_unreal = 0.0
        for op in opening:
            close = None
            if baseline_day:
                close, _src = _day_close(cur, op["symbol"], baseline_day)
            if close is None:
                notes.append(f"baseline close missing for {op['symbol']} — Monday absorbs its carried move")
                close = op["avg_price"]
            prev_unreal += _lot_unreal({"direction": op["direction"], "qty": op["qty"],
                                        "price": op["avg_price"]}, close)

        days_out = []
        fill_idx = 0
        cumulative = 0.0
        for d in week_days:
            if d > today:
                break
            realised_d = 0.0
            n_closed_before = len(closed)
            while fill_idx < len(fills) and fills[fill_idx][0] == d:
                _td, _ts, sym, side, qty, price = fills[fill_idx]
                _apply_fill(books, closed, sym, side, int(qty), float(price), d)
                fill_idx += 1
            realised_d = sum(c["pnl"] for c in closed[n_closed_before:])

            unreal_d = 0.0
            for sym, book in books.items():
                if not book:
                    continue
                close, src = _day_close(cur, sym, d)
                if close is None:
                    notes.append(f"no close for {sym} on {d} — its unrealised carried at entry (0)")
                for lot in book:
                    unreal_d += _lot_unreal(lot, close if close is not None else lot["price"])

            m2m = realised_d + (unreal_d - prev_unreal)
            cumulative += m2m
            days_out.append({"date": str(d), "day": d.strftime("%a"),
                             "realised": round(realised_d, 2),
                             "unrealised_eod": round(unreal_d, 2),
                             "m2m": round(m2m, 2), "cumulative": round(cumulative, 2)})
            prev_unreal = unreal_d

        # 4) end-state open positions from the replay
        open_out = []
        for sym in sorted(books):
            for lot in books[sym]:
                close, src = _day_close(cur, sym, min(today, week_days[-1]))
                open_out.append({
                    "symbol": sym, "direction": lot["direction"], "qty": lot["qty"],
                    "avg_entry": round(lot["price"], 2),
                    "last_close": close,
                    "unrealised": round(_lot_unreal(lot, close), 2) if close is not None else None,
                })

        # 5) checksum vs smartgain_holdings — a mismatch means an orderbook batch
        # is missing; the graph auto-corrects when it lands (spec rule).
        cur.execute("""
            SELECT symbol, direction, qty FROM smartgain_holdings
            WHERE account=%s AND week_start=%s
        """, (account, ws))
        held = {(r[0], r[1]): int(r[2]) for r in cur.fetchall()}
        replay = {(p["symbol"], p["direction"]): p["qty"] for p in open_out}
        mismatch = []
        for k in sorted(set(held) | set(replay)):
            if held.get(k, 0) != replay.get(k, 0):
                mismatch.append({"symbol": k[0], "direction": k[1],
                                 "holdings_qty": held.get(k, 0), "replay_qty": replay.get(k, 0)})
        if mismatch:
            notes.append("replay/holdings mismatch — an orderbook batch is likely missing from smartgain_orders")

    return {
        "account": account, "week_start": str(ws), "week_end": str(week_days[-1]),
        "opening_positions": opening,
        "days": days_out,
        "week_m2m": round(sum(x["m2m"] for x in days_out), 2),
        "week_realised": round(sum(x["realised"] for x in days_out), 2),
        "closed_positions": closed,
        "open_positions": open_out,
        "holdings_mismatch": mismatch,
        "notes": notes,
        "computed_at": datetime.utcnow().isoformat() + "Z",
    }
