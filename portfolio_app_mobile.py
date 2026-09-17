"""
portfolio_app_mobile.py — cc#2194 (founder 17-Sep-2026 15:10 IST): the "My Portfolio" tile in My Scorr
and the /m/portfolio page. The founder's own client portfolio as the web Adaptive Dashboard shows it
per client, then the web /health report re-presented as swipeable mobile cards.

NOTHING IS RE-DERIVED HERE. Two readers, one computation each:
  GET /api/mobile/portfolio?pid=         → the ONE row of hr_endpoints._health_portfolios_inner() for that
                                            pid — the exact function /api/health/portfolios serves the
                                            /adaptive client shelf from. Value, P&L, start date, alpha,
                                            the XIRR trio and the pay-in / payout / net pay-in figures are
                                            that row's own fields, passed through (nulls stay nulls).
  GET /api/mobile/portfolio/clients       → SELECT id, name FROM hr_portfolios ORDER BY name (the client
                                            picker; founder_1515_client_switch).
  the report itself                       → the page reads the EXISTING /api/mobile/health_app/report?pid=
                                            (health_app_mobile.py V2 → hr_report.build_report, the web
                                            /health report's own dict). One source; every rail card is a
                                            field the web page renders.
  PDF                                     → the page calls the EXISTING /api/health/report_pdf_self/{pid}
                                            (hr_report_pdf.py cc#655): a signed link to the white-label
                                            PDF, opened the same way the web SAVE PDF button opens it.
Default pid = 165 (Akshay Gupta, PMS — founder_1515_reference_pid). The page remembers the last pick in
localStorage; ?pid= always wins.

do_not_touch honoured: hr_* tables, hr_report.py, hr_report_pdf.py, the web /health and /adaptive
pages and the SmartGain page are all read-only from here (imports, never edits).
"""
import os
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
import psycopg

from mobile_endpoints import _guard, _json_safe, _page
from hr_endpoints import _health_portfolios_inner

log = logging.getLogger("scorr.mobile.portfolio")
router = APIRouter()

# founder_1515_reference_pid: "Reference portfolio = Akshay Gupta, hr_portfolios id 165 (source PMS).
# Default pid on /m/portfolio = 165."
DEFAULT_PID = 165

# The shelf-row fields the three snapshot cards read. Listed so the payload is a stated contract, and
# so a field the shelf stops sending shows up as a missing key here rather than a silent None.
SNAPSHOT_KEYS = (
    "id", "name", "category", "active", "n_holdings",
    "invested", "current", "cash", "total_portfolio", "pnl", "pnl_pct", "basis", "needs_invested_amount",
    "realised_pnl", "unrealised_pnl", "holdings_cost",
    "start_date", "start_source", "nifty_pct", "alpha_pct",
    "xirr", "nifty_xirr", "alpha_xirr", "xirr_reason",
)
LEDGER_KEYS = ("ledger_rows", "payin_total", "payout_total", "net_payin", "ledger_first_date", "ledger_last_date")

XIRR_REASON_TEXT = {
    "no_ledger": "No ledger on record — XIRR needs dated pay-ins and payouts.",
    "no_payin": "The ledger has no pay-in row, so there is no starting flow to measure from.",
    "too_short": "The ledger is too short to solve a yearly rate honestly.",
    "no_current_value": "No current value to close the flows against.",
}


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def pick_row(rows, pid):
    """The one shelf row for pid, or None. Pure, so it is unit-tested without a database."""
    for r in rows or []:
        if r.get("id") == pid:
            return r
    return None


def shape(row):
    """The page's snapshot payload from ONE shelf row. Every value is the row's own; a null stays a
    null (the shelf's rule: "we could not measure it" and "it was zero" are different statements)."""
    snap = {k: row.get(k) for k in SNAPSHOT_KEYS}
    ledger = {k: row.get(k) for k in LEDGER_KEYS}
    has_ledger = bool(ledger.get("ledger_rows"))
    reason = snap.get("xirr_reason")
    return {
        "pid": row.get("id"), "name": row.get("name"),
        "snapshot": snap,
        "cash_flows": {
            **ledger,
            "has_ledger": has_ledger,
            "period": ("since " + str(ledger["ledger_first_date"])) if has_ledger and ledger.get("ledger_first_date") else None,
            # The shelf card's own marker: net pay-in and the recorded Invested disagree by > Rs.1,000.
            "invested_set_manually": (ledger.get("net_payin") is not None and snap.get("invested") is not None
                                      and abs(float(ledger["net_payin"]) - float(snap["invested"])) > 1000),
        },
        "xirr_note": (None if snap.get("xirr") is not None else XIRR_REASON_TEXT.get(reason, reason)),
        "basis_note": ("Invested amount not set — profit cannot be stated honestly." if snap.get("needs_invested_amount")
                       else "Profit = current value + cash − the amount invested, on the amount invested."),
        "source": "hr_endpoints._health_portfolios_inner — the same row the web Adaptive Dashboard shows for this client",
    }


@router.get("/m/portfolio", response_class=HTMLResponse)
def m_portfolio():
    """cc#2194: My Portfolio (My Scorr tile). Snapshot cards + the health report rail + client picker."""
    return _page("portfolio")


@router.get("/api/mobile/portfolio/clients")
@_json_safe
def mobile_portfolio_clients(request: Request):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM hr_portfolios ORDER BY name")
        rows = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    return {"rows": rows, "count": len(rows), "default_pid": DEFAULT_PID}


@router.get("/api/mobile/portfolio")
@_json_safe
def mobile_portfolio(request: Request, pid: int = 0):
    g = _guard(request)
    if g:
        return g
    pid = int(pid or DEFAULT_PID)
    shelf = _health_portfolios_inner()
    rows = (shelf.get("portfolios") or []) if isinstance(shelf, dict) else []
    row = pick_row(rows, pid)
    if row is None:
        return {"error": f"portfolio {pid} not found", "pid": pid, "default_pid": DEFAULT_PID}
    out = shape(row)
    out["default_pid"] = DEFAULT_PID
    out["report_url"] = f"/api/mobile/health_app/report?pid={pid}"
    out["pdf_link_url"] = f"/api/health/report_pdf_self/{pid}"
    return out
