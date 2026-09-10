"""v8_approved_trades.py -- Home APPROVED TRADES slider.

cc#1928 (10-Sep-2026): SOURCE CORRECTED. cc#1867 wired this to v8_paper_positions status=OPEN --
the whole V8 paper book (22 rows) -- which the founder had NOT approved ("APPROVED TRADES 22 -- I
have not approved them", 10-Sep 13:13). Approval lives ONLY in trade_alerts.approved_at, so:

DATA SOURCE (cc#1928):
    trade_alerts WHERE approved_at IS NOT NULL AND source_engine='V8', each row joined to the
    STILL-OPEN v8_paper_positions row it approved. The join key is the one the approve flow
    writes (trade_alerts_endpoints.py cc#1620 origin link, _origin_v8): source_ref =
    symbol@to_char(entry_ts,'YYYY-MM-DD HH24:MI:SS'). Not guessed -- the same key the Ideas /
    Approved-tab resolvers use. Only positions still OPEN: an approval whose position has since
    closed (moved to v8_paper_trades) drops off this slider -- it belongs to the closed book, not
    to "approved trades open". Zero rows => count 0 and the app's empty state; NEVER a fallback to
    the paper book.

    approved_at / approved_via ride along on every card so the app can print
    "Approved 06 Sep, 16:32" in the slot that used to say "Opened ...".

    Response shape is UNCHANGED from cc#1867 (count / positions[...] / formula) so the app needs
    no structural change; the two approval fields are additive. `approved_alerts_total` is also
    added: how many approved alerts exist at all, so a "count 0 while total 1" reading is
    explainable at a glance (that approval's position closed) rather than looking like a miss.

REUSES THE ONE V8 UNREALISED FORMULA (V8_PNL_CANON_V1, CLAUDE.md rule 13; cc#1762) -- does not
    retype it. v8_book_canon.open_marks() is the SAME latest-intraday-close CMP source the V8
    OPEN BOOK total and the Wall of Trades already read; v8_book_canon.unrealised_rupees() is the
    SAME side-aware (mark-entry)*qty formula, returning None (never a substituted 0.00) when cmp
    is missing -- a stale or missing CMP renders the card WITHOUT a marker and WITHOUT a P&L.

RAIL MARKER FORMULA (cc#1867, verified there; unchanged here):
    left_end=min(stop,target); right_end=max(stop,target);
    marker = (cmp-left_end)/(right_end-left_end)*100, clamped 0-100 -- the LOWER price always
    renders on the LEFT (rail_left_label says which end is the target). Entry renders as a faint
    tick at its own price on the same axis.

POTENTIAL LEFT: (target-cmp)*qty for LONG, (cmp-target)*qty for SHORT -- same sign-aware shape as
    unrealised_rupees but against target instead of cmp. Computed inline here, once.

DO_NOT_TOUCH honoured (cc#1867 + cc#1928): v8_paper_positions and every engine that writes it,
    trade_alerts and the approve flow -- this module only SELECTs. The V8 page and its 22-position
    open book are untouched (nothing in this file is imported by any V8-page-serving module).
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
        # cc#1928: founder-approved V8 entries (trade_alerts.approved_at) joined to the position
        # they approved, on the approve flow's own key (symbol@entry_ts second-precision), and
        # ONLY while that position is still OPEN. v8_paper_positions.entry_ts is a naive IST
        # timestamp, exactly what the flow's to_char() wrote into source_ref -- no tz shift here.
        cur.execute("""SELECT p.symbol, p.side, p.basket, p.qty, p.entry_price, p.target, p.stop_loss,
                              p.entry_ts, a.id AS alert_id, a.approved_at, a.approved_via
                       FROM trade_alerts a
                       JOIN v8_paper_positions p
                         ON p.status = 'OPEN'
                        AND p.symbol = split_part(a.source_ref, '@', 1)
                        AND to_char(p.entry_ts, 'YYYY-MM-DD HH24:MI:SS') = split_part(a.source_ref, '@', 2)
                       WHERE a.approved_at IS NOT NULL
                         AND a.source_engine = 'V8'
                         AND a.kind = 'entry'
                         AND strpos(COALESCE(a.source_ref, ''), '@') > 0
                       ORDER BY a.approved_at DESC, a.id DESC""")
        cols = [d[0] for d in cur.description]
        positions = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM trade_alerts WHERE approved_at IS NOT NULL")
        approved_total = int(cur.fetchone()[0])
        # cc#1762: THE canon CMP source, not a second query -- marked for exactly these symbols.
        marks = v8_book_canon.open_marks(cur, [p["symbol"] for p in positions]) if positions else {}

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
                entry_pct = round(max(0.0, min(100.0, (entry - left_end) / (right_end - left_end) * 100.0)), 1)
            if cmp_v is not None:
                marker_pct = round(max(0.0, min(100.0, (cmp_v - left_end) / (right_end - left_end) * 100.0)), 1)

        out.append({
            "symbol": sym, "side": side, "basket": p["basket"],
            "opened_at": p["entry_ts"].isoformat() if p["entry_ts"] else None,
            "approved_at": p["approved_at"].isoformat() if p["approved_at"] else None,   # cc#1928
            "approved_via": p["approved_via"],                                            # cc#1928
            "alert_id": p["alert_id"],                                                    # cc#1928
            "entry": entry, "entry_pct": entry_pct,
            "cmp": cmp_v, "cmp_as_of": cmp_ts,
            "target": target, "stop_loss": stop,
            "rail_left_label": "target" if (target is not None and stop is not None and target <= stop) else "stop",
            "unrealised": unrealised, "potential_left": potential_left,
            "marker_pct": marker_pct,
        })
    return {
        "count": len(out), "positions": out,
        "approved_alerts_total": approved_total,   # cc#1928: all approvals, open or since closed
        "source": ("trade_alerts.approved_at (source_engine=V8, kind=entry) joined to the still-OPEN "
                   "v8_paper_positions row on source_ref = symbol@entry_ts (cc#1928); never the paper book"),
        "formula": ("v8_book_canon.unrealised_rupees over v8_book_canon.open_marks (cc#1762 canon); "
                    "marker_pct = (cmp-min(stop,target))/(max(stop,target)-min(stop,target))*100, "
                    "clamped 0-100 -- lower price renders left, per rail_left_label"),
    }
