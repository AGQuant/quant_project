"""
mobile_cards_endpoints.py — cc#1090 Sprint 4 · P0 · the shell prototype route
=============================================================================
GET /m/cards -> scorr_cards_preview.html

WHY THIS SHIPS BEFORE ANY PAGE ADOPTS ANYTHING. The founder picked RAISED SLAB out of a
four-variant bevel study, and the property that won it is the PRESS — 5px down, ledge collapsing
to zero, 70ms. That cannot be judged from a screenshot and it cannot be judged from a diff. It has
to be felt with a thumb on glass. So the shells go live on their own route first, on real ladder
data, and the sweep across the app follows the founder's verdict rather than preceding it.

IT ALSO CARRIES THE OPEN DECIDE. Slant geometry (APP_MOTION_MODEL_V1) is NOT ruled app-wide yet —
the card is explicit that it stays on deck cards until the founder says otherwise. So this page
renders one slant deck card beside the two shells, as the thing he is choosing between, and
nothing else in the app adopts slant in the meantime.

ITS OWN ROUTER, one include_router() line in main.py, per rule 5 and per the mistake
v10_page_endpoints.py's own header calls out: cc#1065 parked /m/gvm2 on whichever router was
"small and proven mounted" and it had to be moved later. Not repeating it a third time.

NOT A NAV PAGE. NAV-COMPLETE (session_log 2987) does not apply — this is a reference surface
reached by a direct link, not a screen anyone navigates to. Stated here so a later audit does not
flag it as an unfinished page. It stays live after the sweep as the place the shells are defined
by example.

READ-ONLY. This module writes nothing and owns no data. The page fetches the real GVM ladder
client-side from the existing /api/gvm/company/{symbol} endpoint — no new endpoint, no payload
change, and the same read-per-request file serve /m/v10 and /m/digest already use, so a deploy
serves the new file immediately.
"""

import logging
import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.mobile.cards")

router = APIRouter(tags=["mobile"])

_CARDS_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorr_cards_preview.html")


@router.get("/m/cards", response_class=HTMLResponse)
def m_cards_preview():
    """The card depth system, on glass. Both shells plus one slant deck card."""
    try:
        with open(_CARDS_PAGE, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        # Same shape as the other mobile page routes: explain the failure where it is seen rather
        # than serving a blank screen the founder has to guess about.
        return HTMLResponse(
            "<h3 style='font-family:sans-serif'>Card preview unavailable</h3>"
            "<p style='font-family:sans-serif'>scorr_cards_preview.html is missing from the "
            "deploy.</p>",
            status_code=500,
        )
