"""
v8_futures_book.py — cc#885 NATIVE FUTURES PRICE BOOK (founder idea 07-Aug-2026, P2)

WHY THIS IS ITS OWN FILE AND ITS OWN ENDPOINT
The card's first and hardest requirement is that the EQUITY path stays byte-identical: "zero
change to current queries or rendering". The surest way to honour that is not to touch
/api/paper/status at all. This module answers a separate question on a separate route, and the
dashboard only calls it when the founder actually switches the toggle to Futures. In Equity mode
nothing here runs, no extra query is issued, and the existing markup is produced by the same code
that produced it yesterday.

WHAT "NATIVE FUTURES BOOK" MEANS
Every price is a futures price. Not a basis adjustment applied to the equity book — the actual
fyers_fut bars:
  * CMP    = the latest fyers_fut close for that symbol. The exact inverse of the equity LATERAL
             in main.py:1620, which excludes fyers_fut precisely so the cash book never sees it.
  * ENTRY  = the FIRST fyers_fut close at or after entry_ts — the futures tick of the signal bar.
             Resolved on every render, deterministic, no stored column, no schema change.
  * P&L    = (cmp - entry) * qty * direction, on futures prices at both ends. Same arithmetic as
             the equity book, different inputs.

THE SERIES BOUNDARY, AND THE TRAP INSIDE IT
The fyers_fut feed's first bar is 31-Jul-2026 11:25 IST (verified live: 208 symbols, 80,020 bars).
The founder's ruling is that the futures series starts at the Aug expiry, 31-Jul, and older
periods are ignored. So a position entered BEFORE 31-Jul has no futures entry and is EXCLUDED from
this view, with the count stated on screen. It is never given a derived or fake entry.

But the boundary is not only a date. On 31-Jul the feed did not start until 11:25, so a position
opened at 09:31 that day is inside the series by date and STILL has no futures bar at its signal.
"First bar at or after entry_ts" would hand it the 11:25 bar — 114 minutes late — and print it as
if it were the entry. That is exactly the silent-mixing the card's trap forbids. So an entry bar
is only accepted within ENTRY_LAG_TOLERANCE_MIN of the signal; beyond that the row renders with no
entry, no P&L, and a title saying why. Live today that is one position (ZYDUSLIFE, 31-Jul 09:31);
every other in-series row resolves within five minutes.

BASIS LABELLING (card verify 4)
This is a deliberately DIFFERENT-basis view, so DISPLAY_PARITY 16202 is satisfied by LABELLING
rather than by matching: every number that differs from the equity book is marked FUT, and the
numbers that stay on the equity basis — target, stop, realised P&L — are marked EQ. Target and
stop are ENGINE levels set on the cash price; re-deriving them in futures terms would invent risk
levels the engine never used.

TRAPS OBSERVED:
  * intraday_prices.ts and v8_paper_positions.entry_ts are BOTH naive IST. No conversion anywhere
    in this file (the cc#844 phantom-330-minute class).
  * Context isolation (CLAUDE.md rule 7): v8_paper_* only. tc_intraday_* is never read here.
"""

import os
import logging
from datetime import datetime

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scorr_auth import _is_authed

log = logging.getLogger("scorr.v8_futures_book")
router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")

FUT_SOURCE = "fyers_fut"
# cc#1019 FUT_BOOK_CUTOVER_V1 (session_log 21766): the moment the realised book became a futures
# book. Registry row, not a constant — the founder can move it without a deploy.
BOOK_CUTOVER_KEY = "v8_fut_book_cutover_ts"
# How far after the signal a futures bar may be and still count as "the futures tick of the signal
# bar". Bars are 5-minute buckets and entry_ts is a mid-bucket timestamp, so a real match lands
# under 5 minutes; 10 leaves room for a single skipped bucket without ever accepting the 114-minute
# 31-Jul case that this tolerance exists to reject.
ENTRY_LAG_TOLERANCE_MIN = 10


def _conn():
    return psycopg.connect(DATABASE_URL)


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _f(v):
    return float(v) if v is not None else None


@router.get("/api/v8/futures_book")
def v8_futures_book(request: Request):
    """The open paper book priced on futures. ONE query for the rows, one for the series start.

    Returns the SAME position identity (symbol/side/qty/basket) as the equity book so the
    dashboard can merge by symbol rather than re-render a second table.
    """
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(ts) FROM intraday_prices WHERE source = %s", (FUT_SOURCE,))
            row = cur.fetchone()
            series_start = row[0] if row else None
            if series_start is None:
                return {"error": "no futures bars exist yet", "positions": [], "count": 0}

            # One statement for the whole book. Three LATERALs per position:
            #   fb  the last futures bar at or BEFORE entry  -> the entry leg
            #   fc  the newest futures bar                   -> CMP
            #   fp  the last futures bar of a PRIOR day      -> previous close, for day %
            # ORDER BY entry_ts DESC matches the equity book's own ordering.
            #
            # cc#1019 FUT_BOOK_CUTOVER_V1 (session_log 21766): the entry leg is no longer DERIVED
            # here at all. Since the 14-Aug re-base, v8_paper_positions.entry_price IS the futures
            # price of the fill, so this view reads the book instead of re-deriving a second
            # opinion about it — the double-conversion item 6 exists to remove. `fb` stays as the
            # AUDIT of that: it is the same at-or-before lookup the engine's fill pricing and the
            # re-base used, so if the stored entry does not match it, the fill was recorded on the
            # cash basis and this view says so rather than labelling a cash price FUT. The old
            # at-or-AFTER lookup is gone; it named a bar the fill never saw.
            cur.execute("""
                SELECT p.symbol, p.side, p.basket, p.qty, p.entry_ts,
                       p.entry_price AS stored_entry, p.target, p.stop_loss,
                       fb.close AS fut_bar_at_entry, fb.ts AS fut_bar_ts,
                       fc.close AS fut_cmp,   fc.ts AS fut_cmp_ts,
                       fp.close AS fut_prev_close
                FROM v8_paper_positions p
                LEFT JOIN LATERAL (
                    SELECT close, ts FROM intraday_prices i
                    WHERE i.symbol = p.symbol AND i.source = %s AND i.ts <= p.entry_ts
                    ORDER BY i.ts DESC LIMIT 1
                ) fb ON true
                LEFT JOIN LATERAL (
                    SELECT close, ts FROM intraday_prices i
                    WHERE i.symbol = p.symbol AND i.source = %s
                    ORDER BY i.ts DESC LIMIT 1
                ) fc ON true
                LEFT JOIN LATERAL (
                    SELECT close FROM intraday_prices i
                    WHERE i.symbol = p.symbol AND i.source = %s
                      AND i.ts::date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                    ORDER BY i.ts DESC LIMIT 1
                ) fp ON true
                WHERE p.status = 'OPEN'
                ORDER BY p.entry_ts DESC
            """, (FUT_SOURCE, FUT_SOURCE, FUT_SOURCE, FUT_SOURCE))
            pos = _rows(cur)
            # cc#1019: the realised-book cutover, from the registry. The dashboard reads it from
            # here so the Futures view's Realised / Win-Loss / Trade Log filter on one server-held
            # moment instead of a date literal compiled into the page.
            cur.execute("SELECT value FROM app_config WHERE key = %s", (BOOK_CUTOVER_KEY,))
            _cut = cur.fetchone()
            book_cutover = _cut[0] if _cut and _cut[0] else None
    except Exception as e:
        log.exception("v8 futures book failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "positions": [], "count": 0}

    series_date = series_start.date()
    out, excluded, unrealised = [], [], 0.0
    unresolved = 0

    for p in pos:
        # ── the series boundary. A position opened before the Aug series has no futures entry
        # and is not shown here at all. Counted and named, never silently dropped.
        if p["entry_ts"] is not None and p["entry_ts"].date() < series_date:
            excluded.append({"symbol": p["symbol"],
                             "entry_ts": p["entry_ts"].strftime("%d %b %H:%M")})
            continue

        qty = _f(p["qty"])
        direction = -1.0 if (p["side"] or "").upper() == "SHORT" else 1.0
        cmp_ = _f(p["fut_cmp"])
        stored = _f(p["stored_entry"])
        bar = _f(p["fut_bar_at_entry"])
        entry = None
        gap = None

        # ── cc#1019: the entry leg is the BOOK's entry price, audited against the futures bar that
        # existed at the fill. Three outcomes, and only the first one prints a price.
        lag_min = None
        if p["fut_bar_ts"] is not None and p["entry_ts"] is not None:
            lag_min = (p["entry_ts"] - p["fut_bar_ts"]).total_seconds() / 60.0
        if bar is None or lag_min is None or lag_min > ENTRY_LAG_TOLERANCE_MIN:
            # No futures bar was live at this fill — a halt, an illiquid leg, or a position older
            # than the feed. Never substituted with the cash entry.
            gap = ("No futures bar was live at this entry"
                   + (f" — the newest was {int(lag_min)} min stale" if lag_min is not None else "")
                   + f". The fyers_fut feed began {series_start.strftime('%d %b %H:%M')}. "
                     "Showing no entry rather than a price the fill never saw.")
        elif stored is not None and round(stored, 2) == round(bar, 2):
            # The stored entry IS the futures bar at the fill: futures-priced book entry.
            entry = stored
        else:
            # A futures bar exists but the book holds a different number — the fill fell back to
            # the cash price (the engine logs a WARNING when it does). Saying nothing here would
            # print a cash price under a FUT heading, which is the one thing this view must not do.
            gap = (f"This fill was recorded on the CASH basis ({stored}) — the futures bar at the "
                   f"entry was {bar}. Showing no futures entry rather than labelling a cash price "
                   f"FUT. See the engine's cc#1019 warning for this symbol.")

        pnl = ret = None
        if entry is not None and cmp_ is not None and qty is not None:
            pnl = (cmp_ - entry) * qty * direction
            ret = ((cmp_ - entry) / entry * 100.0 * direction) if entry else None
            unrealised += pnl
        else:
            unresolved += 1

        prev = _f(p["fut_prev_close"])
        day_pct = ((cmp_ - prev) / prev * 100.0) if (cmp_ is not None and prev) else None

        out.append({
            "symbol": p["symbol"],
            "side": (p["side"] or "").upper(),
            "basket": p["basket"],
            "qty": qty,
            # entry_ts is naive IST — formatted, never converted.
            "entry_ts": p["entry_ts"].strftime("%d %b %H:%M") if p["entry_ts"] else None,
            "fut_entry": entry,
            "fut_entry_ts": p["fut_bar_ts"].strftime("%d %b %H:%M") if (entry is not None and p["fut_bar_ts"]) else None,
            # cc#1019: 'book' = the stored entry_price, futures-priced and audited against the bar.
            # 'none' = no futures entry is shown, and `gap` says which of the two reasons applies.
            "entry_source": "book" if entry is not None else "none",
            "fut_cmp": cmp_,
            "fut_cmp_ts": p["fut_cmp_ts"].strftime("%d %b %H:%M") if p["fut_cmp_ts"] else None,
            "fut_pnl": round(pnl, 2) if pnl is not None else None,
            "fut_ret_pct": round(ret, 2) if ret is not None else None,
            "fut_day_pct": round(day_pct, 2) if day_pct is not None else None,
            # Exposed so the dashboard computes DAY% from the same raw pair the equity book uses
            # (cc#367: one hand-verifiable CMP/prev-close pair per row) instead of reconstructing
            # a previous close out of a rounded percentage.
            "fut_prev_close": prev,
            # The engine's CASH decision levels. cc#1019 item 7 (session_log 21769) takes them OFF
            # the Futures tab — a futures view renders zero equity-priced values — but they stay in
            # the payload because the Equity view and the row tooltips still read them, and because
            # dropping a field breaks any consumer that already has it.
            "eq_target": _f(p["target"]),
            "eq_stop": _f(p["stop_loss"]),
            "gap": gap,
        })

    return {
        "basis": "FUT",
        "positions": out,
        "count": len(out),
        "unrealised": round(unrealised, 2) if out else None,
        "unresolved": unresolved,
        "series_start": series_start.strftime("%Y-%m-%d %H:%M"),
        "series_start_date": series_date.isoformat(),
        "excluded_pre_series": len(excluded),
        "excluded": excluded,
        # cc#1019: the realised-book cutover the Futures view filters closed trades on. Served, not
        # compiled into the page, so moving the registry row moves every surface at once.
        "book_cutover_ts": str(book_cutover) if book_cutover else None,
        "book_cutover_key": BOOK_CUTOVER_KEY,
        # The exact sentence the card asks to be on screen (item 5), built from real counts.
        "note": (f"Futures book from {series_date.strftime('%d-%b')} · "
                 f"{len(excluded)} older position{'' if len(excluded) == 1 else 's'} in Equity view"),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
