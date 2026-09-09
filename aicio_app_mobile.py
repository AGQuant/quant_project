"""
aicio_app_mobile.py — cc#1904 APP BUILD: AI CIO section page backend
(design_refs/scorr_app_aicio_R1.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

GATE CHECKED FIRST, per the card's own instruction: is max_chats really the AI CIO store?
Queried information_schema for every table matching chat/cio/conversation/ai_/assistant in the
public schema — max_chats is the ONLY one. Fable's premise holds; the card's evidence (2 rows) is
confirmed live: exactly 2 rows, "max_scorr_arpit" (Max session) and "ask_scorr_arpit" (Ask
session).

"LAST USED" — READ THIS BEFORE TRUSTING THE REF'S "13 Jun" FIGURE. The ref's own closing note
explicitly invites this check: "If the count is wrong... that is a finding worth having before
the build, not after." The COUNT (2) is right. The DATE is not what it looks like: 13-Jun-2026 is
max_scorr_arpit's created_at (when the session row was first made), not when it was last USED.
That same row's updated_at is 2026-08-05 01:54:08 — the chat kept being used for nearly two
months after its creation date. MAX(updated_at) across both rows is 05-Aug-2026, not 13-Jun. This
build reports the TRUE last-activity date (05-Aug), not the ref's created_at-based one — the
surface is still barely used (2 stored conversations, over a month idle as of today), just not
AS stale as "13 Jun" implies. Flagged, not silently reproduced — same discipline as every other
ref-vs-live discrepancy this session (cc#1892, cc#1899).

REACH COUNTS — sectors (128) and result write-ups (774) reused from the SAME live counts cc#1900
and cc#1901 already verified this session (sector_ratings latest score_date, result_analysis_v2
total), not re-derived with a second query text that could drift. Baskets (7) reused from the
same is_active AND basket_name NOT LIKE 'finz_%' registry filter cc#1892's qb_app_mobile.py
already established as the ONE function every caller goes through — mirrored here as a single
query, not imported, since this file has no other qb_app_mobile dependency to justify the import.
Ratings/Trade-scores rows carry no live count in the ref (their "b" slot is a qualitative label,
GVM/TC, not a number) and stay that way here — never inventing a count the ref itself did not ask
for.

do_not_touch honoured: the CIO answering pipeline (scorr_chat_endpoint.py) and the web /cio page —
read-only reference only (grepped to confirm no reusable helper exists for max_chats; none did,
so this file queries it directly rather than importing anything from that pipeline).
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
import psycopg

from mobile_endpoints import _guard, _json_safe, _page

router = APIRouter()


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/m/aicio", response_class=HTMLResponse)
def m_aicio():
    """cc#1904: AI CIO section page. Reachable via the NAV array's mobile More sheet — no prior
    /m/ AI CIO route or Home-grid tile existed (same shape as cc#1903's Mutual Funds page)."""
    return _page("aicio")


@router.get("/api/mobile/aicio_app")
@_json_safe
def mobile_aicio_app(request: Request):
    g = _guard(request)
    if g:
        return g

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(updated_at) FROM max_chats")
        conv_count, last_used = cur.fetchone()

        cur.execute("SELECT COUNT(*) FROM sector_ratings WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)")
        sectors = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM result_analysis_v2")
        results = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM quant_basket_registry WHERE is_active AND basket_name NOT LIKE 'finz\\_%'")
        baskets = cur.fetchone()[0] or 0

    return {
        "status": "ok",
        "key_metrics": {
            "conversations": conv_count or 0,
            "last_used": last_used.date().isoformat() if last_used else None,
            "ratings_reach": "full",
            "sectors": sectors,
            "result_writeups": results,
        },
        "what_it_is": {
            "message": "Ask a question about a stock, a sector or your own portfolio and get an "
                       "answer built from the same data the rest of the app uses.",
            "footnote": "Answers cite what they were built from.",
        },
        "your_conversations": {
            "count": conv_count or 0,
            "last_used": last_used.date().isoformat() if last_used else None,
            "message": (f"Only {conv_count} chats are stored and the last one was active "
                       f"{last_used.date().isoformat()}. This card shows the real count rather "
                       "than an inviting empty state, because the honest read is that the "
                       "surface is barely used.") if conv_count else
                       "No conversations stored yet.",
        },
        "reach": {
            "rows": [
                {"label": "Ratings", "note": "full universe", "value": "GVM"},
                {"label": "Trade scores", "note": "all futures names", "value": "TC"},
                {"label": "Baskets", "note": f"{baskets} on the app", "value": "Quant"},
                {"label": "Sectors", "note": None, "value": str(sectors)},
                {"label": "Results write-ups", "note": None, "value": str(results)},
            ],
            "message": "It answers from stored data only. It does not invent a number that is not there.",
        },
        "limits": {
            "message": "It will not give a number it cannot source, and it will not tell you "
                       "what to buy. It explains what the data says and leaves the decision with you.",
        },
    }
