"""v8_approved_trades.py -- cc#1867: Home APPROVED TRADES slider, replacing the V8 OPEN BOOK
aggregate on the home screen. V8 itself is untouched (do_not_touch); this is a NEW read-only
endpoint for a NEW home surface.

DATA SOURCE -- measured before building, not assumed (card's own stated assumption, confirmed):
    v8_paper_positions WHERE status='OPEN' (22 rows, every row carries entry_price/target/
    stop_loss). NOT trade_alerts -- that table has exactly ONE approved row and its
    trade_alert_levels carries NULL target_price/stop_loss, which would ship an empty slider.

REUSES THE ONE V8 UNREALISED FORMULA (V8_PNL_CANON_V1, CLAUDE.md rule 13; cc#1762) -- does not
    retype it. v8_book_canon.open_marks() is the SAME latest-intraday-close CMP source the V8
    OPEN BOOK total and the Wall of Trades already read; v8_book_canon.unrealised_rupees() is the
    SAME side-aware (mark-entry)*qty formula, returning None (never a substituted 0.00) when cmp
    is missing -- exactly what card item 7 asks for ("a stale or missing CMP renders the card
    WITHOUT a marker and WITHOUT a P&L figure").

RAIL MARKER FORMULA -- a real correction made here, not copied blindly from the Fable Room.
    Fable's withdrawal message (09-Sep-2026, cc#1867 thread) posted a rule AND a "proof" that
    CONTRADICT each other:
      stated rule:  left_end=min(stop,target); right_end=max(stop,target);
                    marker = (cmp-left_end)/(right_end-left_end)*100
      worked proof: BHARTIARTL "(1910.44-1825.10)/(1910.44-1799.16) = 76.7%"
                    -- that expression is (stop-cmp)/(stop-target), a DIFFERENT formula.
    These two do not compute the same number (23.3% vs 76.7%) and only one can be right for a
    rail that always renders the LOWER price on the LEFT (Fable's own stated visual rule).
    Verified by hand with a clean case: SHORT, stop=110, target=90 (target is LEFT, lower).
    If cmp lands exactly ON target (maximum profit), the marker must sit at the LEFT edge (0%
    from the left). The STATED rule gives (90-90)/(110-90)*100 = 0% -- correct, sits at the left
    edge where target is. The PROOF's formula gives (110-90)/(110-90)*100 = 100% -- would place
    the dot at the RIGHT edge (stop's position) while cmp is actually at target on the left: a
    mirrored, wrong marker for every SHORT. This module implements the STATED rule (verified
    correct), not the proof's arithmetic (verified wrong) -- flagged to the Fable Room as its own
    finding, not silently picked.

POTENTIAL LEFT: (target-cmp)*qty for LONG, (cmp-target)*qty for SHORT -- same sign-aware shape as
    unrealised_rupees but against target instead of cmp. Not exported from v8_book_canon (which
    has no notion of "potential left"), computed inline here, once.

DO_NOT_TOUCH honoured: v8_paper_positions and every engine that writes it -- this module only
    SELECTs. The V8 page and its aggregate/list are untouched (nothing in this file is imported
    by v8_endpoints.py or any V8-page-serving module).
"""
import logging
import os
from typing import Optional

import psycopg
from fastapi import APIRouter

import v8_book_canon

log = logging.getLogger("scorr.v8_approved_trades")
DATABASE_URL = os.getenv("DATABASE_URL", "")
router = APIRouter(tags=["v8-approved-trades"])


def _conn():
    return psycopg.connect(DATABASE_URL)


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


@router.get("/api/mobile/home/approved-trades")
def approved_trades():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, side, basket, qty, entry_price, target, stop_loss, entry_ts
                       FROM v8_paper_positions WHERE status='OPEN' ORDER BY entry_ts DESC""")
        cols = [d[0] for d in cur.description]
        positions = [dict(zip(cols, r)) for r in cur.fetchall()]
        marks = v8_book_canon.open_marks(cur)   # cc#1762: THE canon CMP source, not a second query

    out = []
    for p in positions:
        sym = p["symbol"]
        mk = marks.get(sym) or {}
        cmp_v, cmp_ts = mk.get("cmp"), mk.get("cmp_ts")
        entry, target, stop = _f(p["entry_price"]), _f(p["target"]), _f(p["stop_loss"])
        qty, side = _f(p["qty"]), str(p["side"] or "").upper()

        unrealised = v8_book_canon.unrealised_rupees(entry, cmp_v, side, qty)   # None if cmp missing

        potential_left = None
        if cmp_v is not None and target is not None and qty is not None:
            sign = -1.0 if side == "SHORT" else 1.0
            potential_left = round((target - cmp_v) * qty * sign, 2)

        marker_pct = None
        entry_pct = None
        if stop is not None and target is not None and stop != target:
            left_end, right_end = min(stop, target), max(stop, target)
            if entry is not None:
                # cc#1867 (Fable withdrawal): "Entry renders as a faint unlabelled tick at its own
                # price position on the same axis" -- not clamped, entry is always between stop and
                # target by construction of how the position was opened.
                entry_pct = round(max(0.0, min(100.0, (entry - left_end) / (right_end - left_end) * 100.0)), 1)
            if cmp_v is not None:
                marker_pct = round(max(0.0, min(100.0, (cmp_v - left_end) / (right_end - left_end) * 100.0)), 1)

        out.append({
            "symbol": sym, "side": side, "basket": p["basket"],
            "opened_at": p["entry_ts"].isoformat() if p["entry_ts"] else None,
            "entry": entry, "entry_pct": entry_pct,
            "cmp": cmp_v, "cmp_as_of": cmp_ts,
            "target": target, "stop_loss": stop,
            "rail_left_label": "target" if (target is not None and stop is not None and target <= stop) else "stop",
            "unrealised": unrealised, "potential_left": potential_left,
            "marker_pct": marker_pct,
        })
    return {
        "count": len(out), "positions": out,
        "formula": ("v8_book_canon.unrealised_rupees over v8_book_canon.open_marks (cc#1762 canon); "
                    "marker_pct = (cmp-min(stop,target))/(max(stop,target)-min(stop,target))*100, "
                    "clamped 0-100 -- lower price renders left, per rail_left_label"),
    }
