"""
health_app_mobile.py — cc#1902 APP BUILD: Portfolio Health section page backend
(design_refs/scorr_app_health_R1.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

ONE endpoint, GET /api/mobile/health_app. Reads hr_portfolios/hr_holdings/hr_realised/hr_ledger
directly (lightweight — name + live holdings count per portfolio, plus the four Stored totals).
Deliberately does NOT call hr_endpoints.health_portfolios()/_health_portfolios_inner(): that
function does CMP resolution, XIRR and ledger-aggregate work for a per-portfolio P&L figure this
page does not render anywhere (the ref's own KM card has no profit number — "How profit is
measured" is a static explanatory card, not a live figure). Pulling in that whole computation for
data this page never displays would be wasted work, not reuse discipline.

REAL NAMES, NOT THE REF'S MASKED LABELS — READ THIS FIRST. The ref (scorr_app_health_R1.html)
masks every client as "Portfolio 1/2/3" ON PURPOSE, for the repo copy only (its own header
comment says so explicitly). This build reads real names from hr_portfolios, per the card's own
"important" field and V1 ("the page shows real portfolio names, not the masked labels from the
ref"). Verified live: the true top-3 by holding count are Ritesh (26), then a FOUR-WAY TIE at 25
(Vishal Bhosale, Akshay Gupta, Komal Dhawan, Seema Rajesh Gupta - ProfitMart) — the ref's "25 and
25" for slots 2-3 is an illustrative two-name sample of what is actually a wider tie. This build
shows the honest top 6 by holdings count (desc, id as the tie-break), "+8 more", not a copy of
the ref's shorter list.

HR_INVESTED_BASIS_RULE_V1 (item 3) — no per-portfolio headline profit number is rendered
ANYWHERE on this page (matching the ref exactly: the KM card carries no P&L figure, and "How
profit is measured" is static prose, not a computed value). The rule is satisfied by absence, not
by a number that needed checking — but if a live profit figure is ever added here, it MUST reuse
hr_endpoints.health_portfolios()'s own basis logic (meta.invested_amount first, else
holdings-cost when every buy price is known, else no fabricated percentage) rather than a second
copy of that computation, exactly as this docstring's first paragraph explains for why that
function is not imported today.

WHITE-LABEL (item 5) — this app card is Scorr-branded like every other app page; that is correct,
this is CC's own internal tool chrome. What must NEVER say "Scorr" or "GVM" is the actual CLIENT
PDF (hr_report_pdf.py, do_not_touch, already enforces this — verified by reading it, not
assumed) and this page's OWN "What a report covers" card, which states the rule in its own
footer text rather than silently complying with it off-screen.

do_not_touch honoured: hr_report_generate and the PDF builder (hr_report.py, hr_report_pdf.py) —
read-only import of NEITHER; this file writes its own lightweight queries instead, so a change to
either builder cannot silently change this summary. The Adaptive Dashboard surfacing of saved
reports (/adaptive) — untouched; "Open a report ›" links there as a plain href, not a re-
implementation of any part of that page.

ARCHITECTURE FLAG — see mobile/health.html's own header comment and the cc#1902 commit message
for the APP_VS_WEB_AUDIENCE_SPLIT_V1 (session_log 16915) angle: the Home grid's existing
"Portfolio Health" tile deliberately opens the WEB /health page today, a founder-approved named
exception. This build does NOT repoint that tile — /m/health ships reachable by typed URL only,
pending a founder/Fable ruling on whether this card is meant to close that named exception.

10-Sep-2026 (Fable V2 below): the founder ruled — Portfolio Health IS an app section, two parts
(upload, report). The V2 endpoints DO import the /health shelf's own _health_portfolios_inner and
hr_report.build_report, on purpose: one number on both surfaces.
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
import psycopg

from mobile_endpoints import _guard, _json_safe, _page

router = APIRouter()


@router.get("/m/health", response_class=HTMLResponse)
def m_health():
    """cc#1902: Portfolio Health section page. Reachable by typed URL only today — see the
    ARCHITECTURE FLAG in this module's docstring and mobile/health.html's header comment for why
    the Home grid's existing "Portfolio Health" tile is deliberately NOT repointed here."""
    return _page("health")

RAIL_ROW_CAP = 6

REPORT_PAGES = [
    {"label": "Holdings and rating", "page": "Page 1"},
    {"label": "Allocation and concentration", "page": "Page 2"},
    {"label": "Realised and unrealised", "page": "Page 3"},
    {"label": "What to fix", "page": "Page 4"},
]


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/api/mobile/health_app")
@_json_safe
def mobile_health_app(request: Request):
    g = _guard(request)
    if g:
        return g

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.name,
                   COUNT(*) FILTER (WHERE h.symbol IS NOT NULL AND UPPER(h.symbol) != 'CASH') AS n_holdings
            FROM hr_portfolios p LEFT JOIN hr_holdings h ON h.portfolio_id = p.id
            GROUP BY p.id, p.name ORDER BY n_holdings DESC, p.id
        """)
        portfolios = [{"id": r[0], "name": r[1], "n_holdings": r[2]} for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM hr_holdings")
        holdings_rows = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM hr_realised")
        realised_rows = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM hr_ledger")
        ledger_rows = cur.fetchone()[0] or 0

    return {
        "status": "ok",
        "key_metrics": {
            "portfolios_saved": len(portfolios),
            "holdings": holdings_rows,
            "realised_trades": realised_rows,
            "ledger_rows": ledger_rows,
            "report_pages": len(REPORT_PAGES),
        },
        "saved_portfolios": {
            "rows": [{"name": p["name"], "n_holdings": p["n_holdings"], "status": "Ready"}
                     for p in portfolios[:RAIL_ROW_CAP]],
            "count": len(portfolios),
            "more": max(0, len(portfolios) - RAIL_ROW_CAP),
        },
        "report_contents": {
            "rows": REPORT_PAGES,
            "footer": "White-label. Never names Scorr or GVM — the word used is Rating.",
        },
        "stored": {
            "rows": [
                {"label": "Holdings rows", "note": f"across {len(portfolios)} portfolios", "count": holdings_rows},
                {"label": "Realised trades", "note": None, "count": realised_rows},
                {"label": "Ledger entries", "note": None, "count": ledger_rows},
            ],
        },
        "profit_basis": {
            "message": "Headline profit is measured against the amount actually invested, not "
                       "against the market value at the start.",
        },
        "add_portfolio": {
            "message": "Holdings, amount invested, start date and cash. The report builds from "
                       "those four things.",
            "footnote": "Reports appear on the Adaptive Dashboard once saved.",
        },
    }


# ═══ V2 — Fable single mode 10-Sep-2026 (founder: "portfolio health two parts — upload and report;
# report follows the website format, components more intuitive; PDF later"). Additive.
#   GET /api/mobile/health_app/list         → saved portfolios with the headline numbers (invested basis),
#                                              from hr_endpoints._health_portfolios_inner — the /health shelf's own
#   GET /api/mobile/health_app/report?pid=  → the full report, from hr_report.build_report — the web report's own
# Upload and save go straight to the web's POST /api/health/upload and /api/health/generate from the page.
from hr_endpoints import _health_portfolios_inner
from hr_report import build_report


@router.get("/api/mobile/health_app/list")
@_json_safe
def mobile_health_list(request: Request):
    g = _guard(request)
    if g:
        return g
    data = _health_portfolios_inner()
    rows = []
    for p in (data.get("portfolios") or []) if isinstance(data, dict) else []:
        rows.append({"id": p.get("id"), "name": p.get("name"), "n": p.get("n_holdings"), "active": p.get("active"),
                     "invested": p.get("invested"), "current": p.get("current"), "cash": p.get("cash"),
                     "total": p.get("total_portfolio"), "pnl": p.get("pnl"), "pnl_pct": p.get("pnl_pct"),
                     "basis": p.get("basis"), "needs_invested": p.get("needs_invested_amount"),
                     "start": p.get("start_date"), "nifty_pct": p.get("nifty_pct"), "alpha_pct": p.get("alpha_pct"),
                     "xirr": p.get("xirr"), "created": p.get("created_at"), "category": p.get("category")})
    rows.sort(key=lambda r: (not r["active"], -(r["total"] or 0)))
    return {"rows": rows, "count": len(rows), "error": data.get("error") if isinstance(data, dict) else None}


@router.get("/api/mobile/health_app/report")
@_json_safe
def mobile_health_report(request: Request, pid: int = 0):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        rep = build_report(cur, pid)
    if not isinstance(rep, dict) or rep.get("error"):
        return {"error": (rep or {}).get("error", "report unavailable")}
    return rep
