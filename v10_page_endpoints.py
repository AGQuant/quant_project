"""
v10_page_endpoints.py — cc#1069 /m/v10 mobile V10 signal page
=============================================================
Serves scorr_v10_signal.html, the destination of the INDEX SIGNALS card cc#1068 put on the
mobile home page. The page fetches GET /api/v10/signal client-side; this module only serves it.

ITS OWN ROUTER, DELIBERATELY. cc#1065's /m/gvm2 route was parked on scheduler_health_endpoints
because that router was "small and proven-mounted", with a note to relocate it later — the card
for this page says explicitly not to repeat that. A page route living inside a health router is
the kind of thing nobody finds again, and main.py stays wiring-only either way (rule 4), so the
only cost of doing it properly is this file.

READ-ONLY. No state, no DB, no engine contact. The V10 engine, v10_tick and every V8 surface are
untouched.
"""

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["mobile"])

_V10_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorr_v10_signal.html")


@router.get("/m/v10", response_class=HTMLResponse)
def m_v10_signal():
    """The V10 ST+EMA signal view. Read from disk per request rather than cached at import so a
    deploy serves the new file immediately — the page itself is sent no-store by auth_gate, so
    there is no cache layer here to bust."""
    try:
        with open(_V10_PAGE, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        # Say what is missing rather than 500 with a stack trace. The INDEX SIGNALS card links
        # here, so a broken deploy should explain itself on the screen the founder taps into.
        return HTMLResponse(
            "<h3 style='font-family:sans-serif'>V10 view unavailable</h3>"
            "<p style='font-family:sans-serif'>scorr_v10_signal.html is missing from the deploy.</p>",
            status_code=500,
        )
