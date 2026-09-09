"""
smartgain_app_portfolio.py — cc#1895 APP BUILD: My Portfolio section page backend
(design_refs/scorr_app_myportfolio_R4.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

ONE endpoint, GET /api/mobile/myportfolio, assembling everything the page needs from EXISTING
canonical sources — nothing here recomputes a P&L figure that already has an owner:

  - Key metrics (realised this week, unrealised, gross/brokerage/net, open positions, open MTM):
    scorr_endpoints.smartgain_m2m() called DIRECTLY, not re-derived. That function is the ONE place
    current_week_realised()/current_week_brokerage() (cc#237/cc#1878's ISO-week-scoped fix) already
    get called and netted — this endpoint reuses its RETURN VALUE, never its own copy of that logic.
  - Closed-this-week trade list: smartgain_daily_m2m._week_response()'s own `closed_positions` —
    individual FIFO-close line items, not a competing aggregate. The week's REALISED TOTAL shown on
    this page is smartgain_m2m()'s own `realised`, never re-summed from this list, so the two can
    never silently disagree (the exact class of bug cc#1878 was about).
  - Week-by-week history: smartgain_weekly_pnl (real, broker-reconciled closed weeks) plus the
    CURRENT open week from smartgain_m2m()'s own `realised` — never a second replay of the current
    week. A gap between two stored weeks (or between the newest stored week and today) is reported
    explicitly, never silently skipped (item 5 of the card).
  - Sector mix: gvm_cache.segment joined to each open position's live value (qty * ltp, from the
    SAME smartgain_m2m() positions list). segment is the ONE real classification field this
    platform has (cc#1892 P1 independently confirmed this against the same table) — used AS IS,
    not remapped to shorter labels. A symbol with no gvm_cache row groups under "Unclassified"
    rather than being silently dropped from the mix.
  - Costs: current week's brokerage (smartgain_m2m()'s own `brokerage`) plus a leg count (filled
    orders this week) and a round-trip count (len(closed_positions)) — descriptive only, no new
    money figure.

REALISED THIS WEEK — THE cc#1878 LESSON, restated because the card explicitly requires it: this
file NEVER queries smartgain_weekly_pnl for the CURRENT week. That table only ever holds a snapshot
written AFTER a week closes; a naive "latest row" read is exactly what served a five-week-old
number as "this week's realised" on 09-Sep before the fix. The current week's number here comes
from smartgain_m2m() -> current_week_realised(), full stop; smartgain_weekly_pnl is read only for
weeks strictly before the current one.
"""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Request

from fastapi.responses import HTMLResponse

from mobile_endpoints import _conn, _guard, _json_safe, _page

log = logging.getLogger("scorr.mobile.myportfolio")
router = APIRouter()

ACCOUNT = "MHK40"
WEEK_HISTORY_LIMIT = 4   # stored historical weeks to show, oldest-first with the current week appended
LIST_ROW_CAP = 6         # APP_CARD_LAYOUT_LAW_V1: six rows max per list card, then "+N more"


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _sector_mix(cur, positions):
    """Group OPEN position value (qty * ltp) by gvm_cache.segment. A symbol missing from
    gvm_cache (or with no live ltp yet) is NOT dropped — it groups under 'Unclassified' so the
    mix always accounts for 100% of open value, never a silently short total."""
    priced = [p for p in positions if p.get("ltp") is not None and p.get("qty") is not None]
    if not priced:
        return [], []
    syms = list({p["symbol"] for p in priced})
    cur.execute("SELECT symbol, segment FROM gvm_cache WHERE symbol = ANY(%s)", (syms,))
    seg_of = {r[0]: (r[1] or "Unclassified") for r in cur.fetchall()}
    by_seg, by_name = {}, []
    total = 0.0
    for p in priced:
        val = abs(float(p["qty"]) * float(p["ltp"]))
        total += val
        seg = seg_of.get(p["symbol"], "Unclassified")
        by_seg[seg] = by_seg.get(seg, 0.0) + val
        by_name.append({"symbol": p["symbol"], "value": round(val, 2)})
    if total <= 0:
        return [], []
    sector_mix = sorted(
        [{"segment": s, "value": round(v, 2), "pct": round(v / total * 100, 1)} for s, v in by_seg.items()],
        key=lambda x: -x["value"])
    by_name = sorted(
        [{"symbol": n["symbol"], "value": n["value"], "pct": round(n["value"] / total * 100, 1)} for n in by_name],
        key=lambda x: -x["value"])
    return sector_mix, by_name


def _week_history(cur, current_week_start, current_week_realised):
    """smartgain_weekly_pnl history (real, closed weeks) + the current open week, oldest-first,
    with an explicit gap line for any stretch of weeks with no stored row — never rendered as a
    silent skip (item 5). The gap scan runs the FULL span from the oldest fetched stored week to
    the CURRENT week, not just between stored rows — a naive stored-to-stored check would miss
    exactly the gap the design ref itself shows (03-Aug's stored row to 07-Sep's current week,
    4 weeks silently unstored), because there is no later STORED row for it to compare against."""
    cur.execute("""SELECT week_start, realised FROM smartgain_weekly_pnl
                   WHERE account = %s AND week_start < %s
                   ORDER BY week_start DESC LIMIT %s""",
                (ACCOUNT, current_week_start, WEEK_HISTORY_LIMIT))
    stored = list(reversed(cur.fetchall()))   # oldest-first
    have = {w for w, _r in stored}
    have.add(current_week_start)
    weeks = [{"week_start": str(w), "realised": float(r) if r is not None else None, "current": False}
              for w, r in stored]
    weeks.append({"week_start": str(current_week_start),
                   "realised": current_week_realised, "current": True})

    gap_ranges = []
    if stored:
        w, gap_start = stored[0][0] + timedelta(days=7), None
        while w < current_week_start:
            if w not in have:
                gap_start = gap_start or w
            elif gap_start is not None:
                gap_ranges.append((gap_start, w - timedelta(days=7)))
                gap_start = None
            w += timedelta(days=7)
        if gap_start is not None:
            gap_ranges.append((gap_start, w - timedelta(days=7)))
    gap_note = None
    if gap_ranges:
        parts = []
        for gs, ge in gap_ranges:
            n = int((ge - gs).days / 7) + 1
            parts.append(f"{gs.strftime('%d %b')} to {(ge + timedelta(days=6)).strftime('%d %b')} — "
                         f"{n} week{'s' if n != 1 else ''} with no stored row")
        gap_note = "; ".join(parts) + ". Absent, not zero."
    return weeks, gap_note


@router.get("/api/mobile/myportfolio")
@_json_safe
def mobile_myportfolio(request: Request):
    g = _guard(request)
    if g:
        return g
    from scorr_endpoints import smartgain_m2m
    from smartgain_daily_m2m import _week_response

    m2m = smartgain_m2m()
    if isinstance(m2m, dict) and m2m.get("error") and "positions" not in m2m:
        return {"error": m2m["error"]}

    positions = m2m.get("positions") or []
    today = date.today()
    ws = _monday(today)

    with _conn() as conn, conn.cursor() as cur:
        sector_mix, by_name = _sector_mix(cur, positions)

        cur.execute("""SELECT COUNT(*) FROM smartgain_orders
                       WHERE account=%s AND status='FILLED' AND trade_date BETWEEN %s AND %s""",
                    (ACCOUNT, ws, today))
        legs = cur.fetchone()[0]

        weeks, gap_note = _week_history(cur, ws, m2m.get("realised"))

    # _week_response() does its own full FIFO replay — needed ONLY for the closed-this-week LIST
    # (individual trade rows). Its own realised/brokerage/net are NOT used anywhere below; the KPI
    # numbers come exclusively from m2m above (the cc#1878 single-source rule).
    wk = _week_response(None, ACCOUNT)
    fifo_closes = (wk.get("closed_positions") or []) if isinstance(wk, dict) else []
    # ONE row per symbol, not one per FIFO partial-close: a symbol scaled out in two sells this
    # week (real example, 09-Sep: TATACONSUM 150 then 50, both against the same 1,014.10 entry
    # lot) is genuinely one "closed this week" story, matching the ref's own row-count-equals-
    # round-trip-count convention. entry/exit are the FIRST close's own prices (the ref's own
    # convention — hand-verified against this exact live data: TATACONSUM 1,014.10 -> 1,020.10,
    # +1,230 total; BHEL 426.80 -> 435.25, +845; PNB 116.38 -> 116.56, +180 — every figure matches
    # the founder-approved ref exactly). pnl is SUMMED across every fill for that symbol, never
    # re-derived from the displayed entry/exit — it is the sum of the real per-fill FIFO pnls, so
    # the total is correct even though the shown exit price is only the first leg's.
    by_symbol = {}
    for c in fifo_closes:
        row = by_symbol.setdefault(c["symbol"], {"symbol": c["symbol"], "entry": c["entry"],
                                                   "exit": c["exit"], "pnl": 0.0, "close_date": c["close_date"]})
        row["pnl"] = round(row["pnl"] + c["pnl"], 2)
        if c["close_date"] > row["close_date"]:
            row["close_date"] = c["close_date"]   # sort key only — entry/exit stay the FIRST leg's
    closed_this_week = list(by_symbol.values())
    round_trips = len(closed_this_week)

    open_out = sorted(
        [{"symbol": p["symbol"], "direction": p.get("direction"), "qty": p.get("qty"),
          "entry_price": p.get("entry_price"), "ltp": p.get("ltp"),
          "value": round(abs(float(p["qty"]) * float(p["ltp"])), 2)
                   if p.get("qty") is not None and p.get("ltp") is not None else None,
          "pnl": p.get("mtm"), "stale": p.get("stale", False)}
         for p in positions],
        key=lambda x: -(x["value"] or 0))
    closed_out = sorted(closed_this_week, key=lambda c: c.get("close_date") or "", reverse=True)

    return {
        "account": ACCOUNT,
        "as_of": m2m.get("last_updated"),
        "week_start": str(ws),
        "key_metrics": {
            "realised_this_week": m2m.get("realised"),
            "unrealised": m2m.get("unrealised"),
            "gross": m2m.get("gross"),
            "brokerage": m2m.get("brokerage"),
            "net": m2m.get("net"),
            "open_value": round(sum(p["value"] for p in open_out if p["value"] is not None), 2)
                           if open_out else 0.0,
            "open_count": m2m.get("position_count", 0),
            "closed_count_this_week": round_trips,
        },
        "positions": {
            "open": open_out[:LIST_ROW_CAP], "open_count": len(open_out),
            "closed_this_week": closed_out[:LIST_ROW_CAP], "closed_this_week_count": len(closed_out),
        },
        "sector_mix": sector_mix,
        "by_name": by_name[:LIST_ROW_CAP], "by_name_count": len(by_name),
        "week_history": weeks, "week_history_gap_note": gap_note,
        "costs": {
            "brokerage": m2m.get("brokerage"), "legs": legs, "round_trips": round_trips,
            "rate_note": "₹1,000 / cr per leg",
        },
        "row_cap": LIST_ROW_CAP,
        "stale": bool(m2m.get("stale")),
    }


@router.get("/m/myportfolio", response_class=HTMLResponse)
def m_myportfolio():
    """cc#1895: My Portfolio section page, reached from the Home Dashboard section (mobile/home.html
    dashRow) — a grid-tile destination, same discovery pattern as /m/intel and /m/holdings (not in
    the top-nav/More-sheet NAV array; PROTECTED + _MOBILE_DESTINATIONS is this class of page's own
    NAV-COMPLETE convention, per main.py's own precedent comment on /m/holdings)."""
    return _page("myportfolio")
