"""
gvm_twopager.py — cc#1085 · APP_QA_R6 · the GVM 2-Pager print route
====================================================================
GET /gvm/2pager/{symbol} — a standalone, server-rendered, two-A4-page quant note built for
print-to-PDF. Not React, not inside the cio2 shell: the whole point of the sheet is that it
lands on exactly two pages, and that is a property of a plain document with a tuned @page rule,
not of an app shell that happens to be printable.

ITS OWN ROUTER, wired with one include_router() line in main.py, which stays wiring only
(rule 4). The cc#1065 mistake — parking a page route on whichever router was "small and proven
mounted" — is the thing this repo has now corrected twice; not repeating it a third time.

READ-ONLY. This module writes NOTHING. It reads through the EXISTING builder
(gvm_company_report.build_company_report) rather than re-implementing any scoring, so the sheet
and the live /cio2 card can never disagree about a number: there is one computation, consumed
twice. §D of the report is explicit that gvm_scores, screener_raw and input_raw are untouched.

ROUTE, NOT NAV. NAV-COMPLETE (session_log 2987) does not apply — this is a print destination
reached from the 2 Pager button, not a screen anyone navigates to. Stated here so a later audit
does not flag it as an unfinished page.

MARKET CAP TRAP (report §E): page 1 binds mcap from screener_raw.market_cap, which is what the
live GVM page already uses and what reconciles to pe x profit_after_tax. NEVER
gvm_scores.market_cap — that column is stale across the board (BHARATSE reads 1,156.78 Cr there
against 1,557.75 Cr in screener_raw at identical prices). Nothing user-facing is wrong today
because the live page already reads the right one; this note exists so P3 does not bind the
wrong column and quietly introduce the bug.
"""

import logging
import os
from datetime import datetime, timedelta

import psycopg
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.gvm.twopager")

router = APIRouter(tags=["gvm"])


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_today():
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def doc_title(symbol: str, on=None) -> str:
    """SYMBOL_Quant_Note_DDMonYYYY — the browser offers the <title> as the PDF filename, so the
    title IS the filename spec. Kept as its own function because P8 renders four symbols and the
    filename is part of what gets checked."""
    d = on or _ist_today()
    return "%s_Quant_Note_%s" % (symbol.upper(), d.strftime("%d%b%Y"))


def symbol_exists(cur, symbol: str) -> bool:
    """Is this symbol in the LATEST scored set? The report is explicit that unknown means absent
    from the latest gvm_scores.score_date — a symbol scored last month but dropped from the
    universe should 404 rather than render a sheet from stale rows."""
    cur.execute(
        """SELECT 1 FROM gvm_scores
           WHERE symbol = %s AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
           LIMIT 1""",
        (symbol,),
    )
    return cur.fetchone() is not None


_NOT_FOUND = (
    "<!doctype html><meta charset='utf-8'><title>Not found</title>"
    "<body style=\"font-family:Helvetica,Arial,sans-serif;padding:40px;color:#12161C\">"
    "<h2 style='margin:0 0 8px'>No quant note for %s</h2>"
    "<p style='color:#6B7683;margin:0'>That symbol is not in the latest scored universe. "
    "Check the ticker, or open it from the GVM screen and press <b>2 Pager</b>.</p></body>"
)


@router.get("/gvm/2pager/{symbol}", response_class=HTMLResponse)
def gvm_two_pager(symbol: str):
    """The 2-Pager. P1 ships the skeleton — route, 404 and the PDF-filename title; P2 ports the
    locked template from design_refs/scorr_gvm_2pager_R1.html and P3/P4 bind the data."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=404, detail="symbol required")

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                known = symbol_exists(cur, sym)
    except Exception as e:
        # A DB failure is not a missing symbol, and saying "not found" here would be a lie that
        # sends someone hunting for a ticker problem they do not have.
        log.error("gvm_twopager: lookup failed for %s: %s", sym, e, exc_info=True)
        raise HTTPException(status_code=503, detail="scoring data unavailable right now")

    if not known:
        return HTMLResponse(_NOT_FOUND % sym, status_code=404)

    title = doc_title(sym)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>%s</title></head>"
        "<body style=\"font-family:Helvetica,Arial,sans-serif;padding:40px;color:#12161C\">"
        "<p style='color:#6B7683'>Quant note for <b>%s</b> — template lands in R6-P2.</p>"
        "</body></html>" % (title, sym)
    )
