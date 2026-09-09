"""
mobile_watchlist_stub.py — cc#1897 APP BUILD: My Watchlist section page
(design_refs/scorr_app_mywatchlist_R3.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

NO BACKING STORE EXISTS. Fable checked information_schema across the full public schema
(09-Sep-2026): the only tables matching '%watchlist%' are intraday_watchlist and v14_watchlist,
both scanner-candidate logs the engines write, neither a user-personal saved list — re-confirmed
here before writing a line of this page (the card's own gate: "if CC believes a watchlist table
already exists, STOP and report the table name and row count rather than wiring to it").

So this page is PURE STATIC COPY, not a live endpoint — there is no data to fetch, and item 5 of
the card is explicit: do NOT populate any card with sample rows, do NOT hide the tile because it
is empty. Every card below states what is missing, in the ref's own wording, verbatim. No
CREATE TABLE anywhere in this file (item 6) — the store needs a founder decision on owner and
write path first, which is a separate card, not this one.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from mobile_endpoints import _page

router = APIRouter()


@router.get("/m/mywatchlist", response_class=HTMLResponse)
def m_mywatchlist():
    """cc#1897: My Watchlist section page, reached from the Home Dashboard section — a grid-tile
    destination, same discovery pattern as /m/myportfolio (cc#1895) and /m/myalerts (cc#1896)."""
    return _page("mywatchlist")
