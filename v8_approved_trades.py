"""v8_approved_trades.py -- Home APPROVED TRADES slider.

cc#1957 (10-Sep-2026, founder 17:0x "Approved trade give filter icon, 3 options: Future long,
Future short, Equity"): SOURCE WIDENED + ONE BUILDER.

    Until this card the slider read trade_alerts WHERE source_engine='V8' only (cc#1928), joined to
    the V8 paper position -- so an approved EQUITY trade (QB basket, Investment Scanner, Screeners,
    Manual) or an approved TC Scanner / Index Intel signal never reached Home. The Wall of Trades'
    Approved tab (trade_wall_approved.py, cc#1735/1762/1781) already serves EVERY approved alert
    with side, engine, entry_level (= approved_price), cmp, target/stop, pnl through the one canon,
    and open/closed via the trade_alert_levels sidecar. That builder -- trade_wall_approved.
    approved_book() -- is now IMPORTED here, not copied: entry / cmp / pnl / levels on a Home card
    are the SAME numbers the wall shows, to the paise. This module keeps only what the card needs on
    top of that: instrument, the rail geometry, risk/reward left, and the V8 basket tag.

DATA SOURCE (cc#1957):
    approved_book(): trade_alerts WHERE status='approved' LEFT JOIN trade_alert_levels, every
    engine. Home keeps the rows whose resolved close state is OPEN (sidecar closed_at IS NULL /
    no engine exit mirrored). Zero rows => count 0 and the app's empty state; NEVER a fallback to
    the paper book.

INSTRUMENT per row: FUTURES when the symbol is in futures_universe WHERE is_active -- the same
    bare-symbol test the Wall of Trades classifier uses (trade_wall_endpoints.py) -- else EQUITY.
    `side` (LONG / SHORT) rides along from the builder. The three app filters are
    Futures Long = FUTURES & LONG, Futures Short = FUTURES & SHORT, Equity = EQUITY any side;
    `counts` in the payload carries all four numbers so the chip and the DB check agree.

QTY: a V8-linked row carries its position qty from the builder (rupees = position-sized). A row
    with no paper position (equity / manual / TC) has qty None and every rupee here is PER SHARE --
    the card footer prints "per share" where a futures card prints "1 lot (n)". risk_left /
    reward_left / potential_left use the same multiplier (qty, or 1 per share); they are rail
    geometry against the builder's own cmp / target / stop, not a second P&L formula.

RAIL MARKER FORMULA (cc#1867, unchanged): left_end=min(stop,target); right_end=max(stop,target);
    marker = (cmp-left_end)/(right_end-left_end)*100, clamped 0-100 -- the LOWER price always
    renders on the LEFT (rail_left_label says which end is the target). The engine entry (V8 only,
    read by the same source_ref key the approve flow writes) renders as a faint tick.

DO_NOT_TOUCH honoured: trade_wall_approved.py's builder is imported read-only (its loop was moved
    into approved_book() line-for-line, no logic change -- see that file); v8_paper_positions,
    trade_alerts and the approve flow are only SELECTed. The V8 page and the Wall of Trades are
    untouched.
"""
import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter

from mobile_endpoints import _conn, _ist_now
from trade_wall_approved import approved_book   # cc#1957: THE approved-book builder, not a copy

try:
    from nse_holidays import is_trading_day as _is_trading_day   # cc#1962: prev-close date reconciliation
except Exception:   # pragma: no cover
    _is_trading_day = None

log = logging.getLogger("scorr.v8_approved_trades")
router = APIRouter(tags=["v8-approved-trades"])


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def _prev_session(today: date) -> Optional[date]:
    """cc#1962: the immediately preceding NSE trading day (weekends + nse_holidays), or None when the
    holiday module is unavailable -- the flag then reads None, never a guess."""
    if _is_trading_day is None:
        return None
    d = today - timedelta(days=1)
    for _ in range(10):
        if _is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return None


def _iso_ist(stamp) -> Optional[str]:
    """The builder stamps approved_at as naive IST 'YYYY-MM-DD HH:MM'. The card pins its clock to
    Asia/Kolkata (apTrWhenIst), so hand it an unambiguous ISO string with the +05:30 offset --
    a naive string would be read in the DEVICE zone and drift on any phone not set to IST."""
    if not stamp:
        return None
    t = str(stamp)
    return (t.replace(" ", "T") + ":00+05:30") if (len(t) == 16 and t[10] == " ") else t


@router.get("/api/mobile/home/approved-trades")
def approved_trades():
    with _conn() as conn, conn.cursor() as cur:
        book = approved_book(cur, conn)
        rows = [r for r in book["rows"] if not r.get("closed")]
        syms = sorted({r["symbol"] for r in rows})
        futs = set()
        if syms:
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active AND symbol = ANY(%s)", (syms,))
            futs = {x[0] for x in cur.fetchall()}
        # cc#1962 DAY P&L: previous close per symbol from raw_prices -- the SAME DISTINCT ON shape
        # mobile_home2.mobile_breadth uses (most recent row BEFORE today), with its DATE exposed so the
        # sheet can state its own basis: raw_prices coverage is uneven and "most recent earlier close"
        # is not always yesterday's. A symbol with no prior row keeps None everywhere -- never 0.
        today = _ist_now().date()
        prev_session = _prev_session(today)
        pclose = {}
        if syms:
            try:
                cur.execute("""SELECT DISTINCT ON (symbol) symbol, price_date, close
                               FROM raw_prices
                               WHERE symbol = ANY(%s) AND price_date < %s
                               ORDER BY symbol, price_date DESC""", (syms, today))
                for sym_, pd_, close_ in cur.fetchall():
                    pclose[sym_] = {"close": _f(close_), "date": pd_}
            except Exception as e:
                log.warning("cc#1962 prev-close lookup failed: %s", e)
        # V8 rows only: basket tag + engine entry for the rail tick, on the approve flow's own key
        # (source_ref = symbol@entry_ts second-precision, cc#1928). SELECT only.
        v8 = {}
        refs = sorted({r["source_ref"] for r in rows if (r.get("engine") == "V8" and r.get("source_ref"))})
        if refs:
            try:
                cur.execute("""SELECT symbol || '@' || to_char(entry_ts, 'YYYY-MM-DD HH24:MI:SS') AS ref,
                                      basket, entry_price, entry_ts
                               FROM v8_paper_positions
                               WHERE status = 'OPEN' AND entry_ts IS NOT NULL
                                 AND symbol || '@' || to_char(entry_ts, 'YYYY-MM-DD HH24:MI:SS') = ANY(%s)""", (refs,))
                for ref, basket, ep, ets in cur.fetchall():
                    v8.setdefault(ref, {"basket": basket, "entry": _f(ep), "entry_ts": ets})
            except Exception as e:
                log.warning("cc#1957 V8 basket lookup failed: %s", e)

    out = []
    counts = {"all": 0, "futures_long": 0, "futures_short": 0, "equity": 0}
    for r in rows:
        sym, side = r["symbol"], str(r.get("side") or "").upper()
        sign = -1.0 if side == "SHORT" else 1.0
        instrument = "FUTURES" if sym in futs else "EQUITY"
        counts["all"] += 1
        if instrument == "EQUITY":
            counts["equity"] += 1
        elif side == "SHORT":
            counts["futures_short"] += 1
        else:
            counts["futures_long"] += 1

        approved_px, cmp_v = _f(r.get("entry_level")), _f(r.get("cmp"))
        target, stop, qty = _f(r.get("target_price")), _f(r.get("stop_loss")), _f(r.get("qty"))
        mult = qty if qty is not None else 1.0
        lk = v8.get(r.get("source_ref")) if r.get("engine") == "V8" else None
        entry = lk["entry"] if lk else None

        risk_left = round((cmp_v - stop) * mult * sign, 2) if (cmp_v is not None and stop is not None) else None
        reward_left = round((target - cmp_v) * mult * sign, 2) if (cmp_v is not None and target is not None) else None
        rr_left = round(reward_left / risk_left, 1) if (risk_left and reward_left is not None and risk_left > 0) else None
        potential_left = reward_left
        # cc#1962: the % beside "value left" = the move still required from the CURRENT price to the
        # target, sign-aware -- the same basis value-left measures. Stated on the sheet itself.
        reward_left_pct = (round((target - cmp_v) / cmp_v * 100.0 * sign, 2)
                           if (cmp_v not in (None, 0) and target is not None) else None)
        # cc#1962 DAY P&L against the prior close: same mult (position qty, or 1 per share) and sign.
        pc = pclose.get(sym) or {}
        prev_close, prev_date = pc.get("close"), pc.get("date")
        day_pnl = day_pnl_pct = None
        if cmp_v is not None and prev_close not in (None, 0):
            day_pnl = round((cmp_v - prev_close) * mult * sign, 2)
            day_pnl_pct = round((cmp_v - prev_close) / prev_close * 100.0 * sign, 2)
        prev_is_prior_session = (None if (prev_date is None or prev_session is None) else (prev_date == prev_session))

        marker_pct = entry_pct = None
        if stop is not None and target is not None and stop != target:
            left_end, right_end = min(stop, target), max(stop, target)
            if entry is not None:
                entry_pct = round(max(0.0, min(100.0, (entry - left_end) / (right_end - left_end) * 100.0)), 1)
            if cmp_v is not None:
                marker_pct = round(max(0.0, min(100.0, (cmp_v - left_end) / (right_end - left_end) * 100.0)), 1)

        out.append({
            "symbol": sym, "side": side, "instrument": instrument,                      # cc#1957
            "engine": r.get("engine"), "basket": (lk or {}).get("basket"),
            # cc#1962: entry_ts is naive IST -- stamped +05:30 like approved_at so the sheet's IST clock holds on any device
            "opened_at": ((lk["entry_ts"].replace(microsecond=0).isoformat() + "+05:30") if (lk and lk.get("entry_ts")) else None),
            "approved_at": _iso_ist(r.get("approved_at")), "approved_via": r.get("approved_via"),
            "alert_id": r.get("id"),
            "entry": entry, "entry_pct": entry_pct,
            "cmp": cmp_v, "cmp_as_of": r.get("cmp_date"), "cmp_label": r.get("cmp_label"),
            "target": target, "stop_loss": stop,
            "rail_left_label": "target" if (target is not None and stop is not None and target <= stop) else "stop",
            "marker_pct": marker_pct, "potential_left": potential_left,
            "approved_price": approved_px, "qty": qty,
            "qty_basis": r.get("qty_basis"), "per_share": qty is None,                   # cc#1957
            "pnl_since_approval": _f(r.get("pnl")), "pnl_since_approval_pct": _f(r.get("pnl_pct")),   # the builder's own
            "risk_left": risk_left, "reward_left": reward_left, "rr_left": rr_left,
            "reward_left_pct": reward_left_pct,                                            # cc#1962
            "day_pnl": day_pnl, "day_pnl_pct": day_pnl_pct,                                # cc#1962: vs prev close
            "prev_close": prev_close,                                                       # cc#1962
            "prev_close_date": (prev_date.isoformat() if prev_date else None),             # cc#1962: the basis, stated
            "prev_close_is_prior_session": prev_is_prior_session,                          # cc#1962: True = the immediately preceding trading day
        })
    n_prev = sum(1 for p in out if p["prev_close"] is not None)
    n_prior = sum(1 for p in out if p["prev_close_is_prior_session"] is True)
    return {
        "count": len(out), "positions": out, "counts": counts,
        "prev_close_basis": {   # cc#1962: how many rows resolve a prev close, and how many of those are the prior session
            "rows_with_prev_close": n_prev, "rows_prior_session": n_prior,
            "prior_session": (prev_session.isoformat() if prev_session else None),
            "rule": "raw_prices DISTINCT ON (symbol) price_date < today ORDER BY price_date DESC (mobile_breadth shape); "
                    "prev_close_date on each row says which close it is",
        },
        "approved_alerts_total": int(book.get("count") or 0),   # every approval, open or closed
        "source": ("trade_wall_approved.approved_book() -- every approved alert (all engines), rows whose "
                   "resolved close state is OPEN (cc#1957); never the paper book"),
        "instrument_rule": "FUTURES if symbol in futures_universe WHERE is_active (the Wall of Trades test), else EQUITY",
        "pnl_rule": book.get("pnl_rule"), "qty_rule": book.get("qty_rule"), "close_rule": book.get("close_rule"),
        "formula": ("pnl_since_approval / _pct are the builder's own; risk/reward/potential = (level - cmp) x qty "
                    "(1 per share) sign-aware; marker_pct = (cmp-min(stop,target))/(max-min)*100 clamped 0-100 "
                    "-- lower price renders left, per rail_left_label"),
    }
