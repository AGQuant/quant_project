"""
mobile_scanners.py — /m/tcscan (TC Scanner trades) and /m/invscan (Investment Scanner) app pages.
Fable single-mode build 10-Sep-2026. Pages mobile/tcscan.html, mobile/invscan.html; data
/api/mobile/tcscan and /api/mobile/invscan (scanners_app_mobile.py). Same _page() pattern as
mobile_dash_hub.py. Registered per NAV-COMPLETE (2987) in main.py: include_router, PROTECTED,
NAV_REGISTRY; Home grid tiles repointed from /m/check#scan and /inv-scanner.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from mobile_endpoints import _page

router = APIRouter()


@router.get("/m/tcscan", response_class=HTMLResponse)
def m_tcscan():
    return _page("tcscan")


@router.get("/m/invscan", response_class=HTMLResponse)
def m_invscan():
    return _page("invscan")
