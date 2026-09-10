"""
mobile_qb_holdings.py — /m/qb/holdings?basket=<name>: the View-holdings sortable table for one
Quant Basket (QB_APP_R5_LOCK, session_log 42805). Page file mobile/qb_holdings.html; data
/api/mobile/qb_app/holdings (qb_app_mobile.py). Same _page() pattern as mobile_dash_hub.py.
Registered per NAV-COMPLETE (2987): PROTECTED + NAV_REGISTRY in main.py (grid-tile tier — reached
only from the basket detail page, not the nav).
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from mobile_endpoints import _page

router = APIRouter()


@router.get("/m/qb/holdings", response_class=HTMLResponse)
def m_qb_holdings():
    return _page("qb_holdings")
