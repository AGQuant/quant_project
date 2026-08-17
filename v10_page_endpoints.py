"""
v10_page_endpoints.py — cc#1069 /m/v10 mobile page + (Fable 17-Aug) /m/digest
=============================================================================
Serves the app's two distributed sections per APP_SECTION_DISTRIBUTION_V1 (session_log 23903):

  GET /m/v10    -> scorr_v10_signal.html      INDEX INTEL — the desk (V10 renamed in-app).
                   Implements design_refs/scorr_indexintel_mobile_R1.html (bb2fde7).
  GET /m/digest -> scorr_digest_mobile.html   DAILY DIGEST — the morning read.
                   Implements design_refs/scorr_digest_mobile_R1.html (b8949f6).

ITS OWN ROUTER, DELIBERATELY. cc#1065's /m/gvm2 route was parked on scheduler_health_endpoints
because that router was "small and proven-mounted", with a note to relocate it later — the card
for this page says explicitly not to repeat that. A page route living inside a health router is
the kind of thing nobody finds again, and main.py stays wiring-only either way (rule 4), so the
only cost of doing it properly is this file. /m/digest lives here rather than in a third router
because both routes are the same shape (serve one mobile page file, read-only) and one mobile
page router is easier to find than two.

READ-ONLY. No state, no DB, no engine contact. The V10 engine, v10_tick and every V8 surface are
untouched. Data reaches the pages client-side: /m/v10 from the v10/v8 APIs, /m/digest from the
single /api/digest/v3 payload (digest_v3.py).
"""

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["mobile"])

_DIR = os.path.dirname(os.path.abspath(__file__))
_V10_PAGE = os.path.join(_DIR, "scorr_v10_signal.html")
_DIGEST_PAGE = os.path.join(_DIR, "scorr_digest_mobile.html")


def _serve(path: str, what: str) -> HTMLResponse:
    """Read from disk per request rather than cached at import so a deploy serves the new file
    immediately — the pages are sent no-store by auth_gate, so there is no cache layer to bust.
    A missing file says what is missing rather than 500 with a stack trace: these screens are
    reached from home-page cards, so a broken deploy should explain itself where it is seen."""
    try:
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse(
            f"<h3 style='font-family:sans-serif'>{what} unavailable</h3>"
            f"<p style='font-family:sans-serif'>{os.path.basename(path)} is missing from the deploy.</p>",
            status_code=500,
        )


@router.get("/m/v10", response_class=HTMLResponse)
def m_v10_signal():
    """INDEX INTEL — the live desk (V10 renamed in the app)."""
    return _serve(_V10_PAGE, "Index Intel")


@router.get("/m/digest", response_class=HTMLResponse)
def m_digest():
    """DAILY DIGEST — the morning read."""
    return _serve(_DIGEST_PAGE, "Daily Digest")
