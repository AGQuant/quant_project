"""
mobile_dash_hub.py — the app DASHBOARD hub, /m/dash (founder 10-Sep-2026, Fable single-mode push).

The Home grid "Dashboard" tile used to open the WEB V8 Adaptive Dashboard (/dashboard). It now
opens this page: four entries — My Portfolio, My Alerts, My Watchlist, My Trades. Static shell;
the only live number on it is read from /api/mobile/myportfolio, the same endpoint /m/myportfolio
uses (one source, no re-derivation). Page file: mobile/dash.html. Same _page() pattern as
mobile_watchlist_stub.py (cc#1897). Registered per NAV-COMPLETE (2987): PROTECTED + NAV_REGISTRY
in main.py, NAV array in pwa_endpoints.py.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from mobile_endpoints import _page

router = APIRouter()


@router.get("/m/dash", response_class=HTMLResponse)
def m_dash():
    """App Dashboard hub — reached from the Home grid Dashboard tile."""
    return _page("dash")
