"""v8_daylog_extras.py — cc#1561 · Day Log P&L series + return facts for the chart and popover.

ONE SOURCE, NOT A SECOND FORMULA
    The Day Log tab already has a handler (v8_endpoints.v8_daylog) that owns the fresh-era window
    (entry_ts >= app_config.v8_paper_rebuild_cutover_ts), the retired-basket registry exclusion
    (rule 13) and the Rs.500-per-closed-trade brokerage. This file CALLS that handler and folds
    its day rows into a cumulative series. It never re-derives a window start, a capital base or
    a brokerage rule, so the chart cannot drift from the KPI boxes it sits under: gross_total,
    brokerage_total and net_total here ARE summary.total_gross_pnl / total_brokerage /
    total_net_pnl from that payload.

WINDOW START
    There is no typed start date anywhere (discovery, cc_task_logs cc#1561). The table's first
    row is the first fresh-era ENTRY; the P&L series can only begin on the first EXIT, which is
    the first day with closed > 0. window_start is that day, derived from the rows. table_start
    (first row) ships alongside so nothing is hidden.

CAGR
    ((1 + return_pct/100) ** (365 / calendar_days) - 1) * 100. Null when calendar_days < 7. On a
    six-week window this flatters a lot, so cagr_note always rides with it and the surface must
    print it. Never fabricated: if there are no closed trades the series is empty and every
    money figure is 0, return_pct 0, cagr_pct null.

READ PATH ONLY. Nothing here writes.
"""

from datetime import date

from fastapi import APIRouter, HTTPException

import v8_endpoints

router = APIRouter(prefix="/api/v8", tags=["v8"])

CAGR_NOTE = "Annualised from a short window; indicative only."
MIN_CAGR_DAYS = 7


def _d(s):
    """'YYYY-MM-DD' -> date. Day Log dates are already IST calendar days (exit_ts::date)."""
    return date.fromisoformat(str(s)[:10])


def build_series(payload: dict) -> dict:
    """Fold a v8_daylog payload into the series shape. Pure; no DB. Kept separate so it can be
    checked against a captured payload without a connection."""
    days = sorted((payload.get("days") or []), key=lambda r: str(r["date"]))
    summary = payload.get("summary") or {}
    capital = int(payload.get("capital_base") or 5_000_000)

    points, gross_cum, net_cum = [], 0.0, 0.0
    for r in days:
        closed = int(r.get("closed") or 0)
        if closed <= 0:
            continue                       # an entry-only day has no P&L event to plot
        g = float(r.get("gross_pnl") or 0.0)
        n = float(r.get("net_pnl") or 0.0)
        gross_cum += g
        net_cum += n
        points.append({"date": str(r["date"])[:10],
                       "gross_day": int(round(g)), "net_day": int(round(n)),
                       "gross_cum": int(round(gross_cum)), "net_cum": int(round(net_cum))})

    table_start = str(days[0]["date"])[:10] if days else None
    window_start = points[0]["date"] if points else table_start
    window_end = str(days[-1]["date"])[:10] if days else None
    calendar_days = ((_d(window_end) - _d(window_start)).days + 1) if (window_start and window_end) else 0

    gross_total = float(summary.get("total_gross_pnl") or 0.0)
    brokerage_total = int(summary.get("total_brokerage") or 0)
    net_total = float(summary.get("total_net_pnl") or 0.0)
    return_pct = round(net_total / capital * 100, 2) if capital else 0.0

    cagr_pct = None
    if calendar_days >= MIN_CAGR_DAYS:
        cagr_pct = round(((1 + return_pct / 100) ** (365 / calendar_days) - 1) * 100, 2)

    return {
        "window_start": window_start,
        "window_end": window_end,
        "table_start": table_start,          # first row of the Day Log table (first fresh-era entry)
        "era_cutover_ts": payload.get("rebuild_cutover_ts"),
        "view": payload.get("view"),
        "trading_days": len(days),           # the same count the tab prints ("32 trading days")
        "calendar_days": calendar_days,
        "capital": capital,
        "gross_total": int(round(gross_total)),
        "brokerage_total": brokerage_total,
        "net_total": int(round(net_total)),
        "return_pct": return_pct,
        "cagr_pct": cagr_pct,
        "cagr_note": CAGR_NOTE,
        "points": points,
    }


@router.get("/daylog/series")
def v8_daylog_series(view: str = "equity"):
    """cc#1561: cumulative gross/net P&L by exit date plus the return facts behind the Overall
    Return box. Same window, era, registry and brokerage as /api/v8/daylog because it IS that
    payload, folded. ?view=futures follows the Day Log tab's futures book the same way."""
    try:
        payload = v8_endpoints.v8_daylog(era="fresh", view=view)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"v8_daylog_series failed: {e}")
    return build_series(payload)
