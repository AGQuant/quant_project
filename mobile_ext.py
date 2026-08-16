"""
mobile_ext.py — cc#892 breadth screens + cc#893/894 depth endpoints (Fable direct push,
CHARTER_OVERRIDE_08AUG2026, session_log 17783; program MOBILE_REBUILD_IN_PLACE_V1 17782).

OWN FILE, wired by the preview_endpoints tail shim exactly like mobile_home2 — the shim moves to
main.py in the FINAL maintenance pass (founder 08-Aug: Fable does it last; CC stood down).

What lives here and what deliberately does NOT:
  * /m/screeners, /m/sector, /m/holdings, /m/fpc page routes (templates in mobile/).
  * /api/mobile/holdings — smartgain_holdings rows (the ONLY breadth source with no existing API).
  * /api/mobile/result_analysis(+_index) — plain-words analysis from result_analysis_v2.
  * /api/mobile/trades — cc#894 items 5+8, PARITY-FIXED 08-Aug (founder caught all-time vs web
    fresh-book drift): defaults to the WEB MASTER DASHBOARD scope — entry_ts >=
    app_config.v8_paper_rebuild_cutover_ts (cc#504/cc#510 era doctrine), retired baskets
    excluded — with a summary block computed the web way: realised NET of Rs.500/closed-trade
    brokerage, W/L = clean result 'TARGET' vs 'SL'. Verified vs DB: 33 trades, gross 366,112,
    net 349,612, 13/5 — identical to the web cards. ?era=all shows the full legacy book, same as
    the web's own daylog escape hatch. entry_ts/exit_ts naive IST — read raw, never converted.
  * NO screeners/sector endpoints: templates call the EXISTING web APIs (16202 by construction).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mobile_endpoints import (
    rail_state, basket_label, _conn, _rows, _ist_now, _guard, _json_safe, _page,
)

log = logging.getLogger("scorr.mobile.ext")
router = APIRouter()

from v8_book_canon import book_canon, retired_baskets   # cc#970: the ONE book formula (rule 13)
from price_sources import NOT_FUT_SQL   # cc#1056 / cc#1053 source registry — one list, never retyped

BROKERAGE_PER_TRADE = 500       # web daylog doctrine: Rs.500 per closed trade


@router.get("/api/mobile/holdings")
@_json_safe
def mobile_holdings(request: Request):
    """My Portfolio, full screen — every SmartGain holding with entry, LTP and MTM."""
    g = _guard(request)
    if g:
        return g
    now = _ist_now()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, direction, qty, entry_price, ltp, mtm,
                   (updated_at AT TIME ZONE 'Asia/Kolkata') AS updated_at
            FROM smartgain_holdings
            ORDER BY ABS(COALESCE(mtm, 0)) DESC
        """)
        rows = _rows(cur)

    def f(v):
        return float(v) if v is not None else None

    newest = max((r["updated_at"] for r in rows if r["updated_at"]), default=None)
    out = []
    for r in rows:
        entry, ltp, qty = f(r["entry_price"]), f(r["ltp"]), f(r["qty"])
        out.append({
            "symbol": r["symbol"],
            "direction": (r["direction"] or "").upper(),
            "qty": qty, "entry": entry, "ltp": ltp,
            "mtm": f(r["mtm"]),
            "value": round(qty * ltp, 2) if qty is not None and ltp is not None else None,
            "ret_pct": (round((ltp - entry) / entry * 100.0
                              * (-1.0 if (r["direction"] or "").upper().startswith("S") else 1.0), 2)
                        if entry and ltp is not None else None),
        })
    total_mtm = sum((x["mtm"] or 0.0) for x in out) if out else None
    total_val = sum((x["value"] or 0.0) for x in out) if out else None
    return {
        "empty": not out,
        "rows": out, "count": len(out),
        "total_mtm": round(total_mtm, 2) if total_mtm is not None else None,
        "total_value": round(total_val, 2) if total_val is not None else None,
        "message": "No positions open." if not out else None,
        "rail": rail_state(newest, 1440, now, now.weekday() < 5),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/api/mobile/result_analysis")
@_json_safe
def mobile_result_analysis(request: Request, symbol: str = ""):
    """cc#893 item 2 — the plain-words result analysis the web Results Corner shows, per symbol.
    Newest quarter first. Absent symbol answers honestly with found:false, never a stub text."""
    g = _guard(request)
    if g:
        return g
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol required"}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, quarter, analysis_text,
                   (polished_at AT TIME ZONE 'Asia/Kolkata') AS polished_at
            FROM result_analysis_v2
            WHERE symbol = %s
            ORDER BY polished_at DESC NULLS LAST
            LIMIT 1
        """, (sym,))
        rows = _rows(cur)
    if not rows:
        return {"symbol": sym, "found": False}
    r = rows[0]
    return {
        "symbol": r["symbol"], "found": True,
        "quarter": r["quarter"],
        "text": r["analysis_text"],
        "polished_at": r["polished_at"].strftime("%d %b %H:%M") if r["polished_at"] else None,
    }


@router.get("/api/mobile/result_analysis_index")
@_json_safe
def mobile_result_analysis_index(request: Request):
    """Which symbols HAVE analysis — so the Results screen only shows a tap affordance where
    tapping will actually produce something (no dead affordances, cc#893 verify c)."""
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM result_analysis_v2")
        rows = _rows(cur)
    return {"symbols": [r["symbol"] for r in rows], "count": len(rows)}


@router.get("/api/mobile/trades")
@_json_safe
def mobile_trades(request: Request, limit: int = 20, days: int = 30, era: str = "fresh"):
    """cc#894 items 5+8 — the ledger, WEB-PARITY scope (see module header):
    trades: last-N closed trades; day_log: day-by-day P&L; summary: the Master Dashboard cards
    (trades, gross, brokerage, net realised, TARGET/SL wins/losses). era=fresh (default) matches
    the web book; era=all is the full legacy history, like the web's own escape hatch."""
    g = _guard(request)
    if g:
        return g
    limit = max(1, min(limit, 100))
    days = max(1, min(days, 120))
    now = _ist_now()
    with _conn() as conn, conn.cursor() as cur:
        cutover = None
        if era != "all":
            cur.execute("SELECT value FROM app_config WHERE key='v8_paper_rebuild_cutover_ts'")
            _row = cur.fetchone()
            cutover = _row[0] if _row and _row[0] else None
        _retired, _ = retired_baskets(cur)              # cc#970: registry, not a name literal
        scope = {"cut": cutover, "retired": _retired}
        cur.execute("""
            SELECT symbol, side, basket, entry_price, exit_price, qty, pnl, return_pct,
                   result, entry_ts, exit_ts
            FROM v8_paper_trades
            WHERE (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND NOT (basket = ANY(%(retired)s))
            ORDER BY exit_ts DESC NULLS LAST, id DESC
            LIMIT """ + str(limit), scope)
        tr = _rows(cur)
        cur.execute("""
            -- cc#970: a WIN is result='TARGET' and a LOSS is result='SL', here too. This strip
            -- counted pnl>0 / pnl<0, which is a different question and gives a different answer
            -- (on the fresh book: 21W/12L by pnl vs 13W/5L by verdict). One definition of a win
            -- per rule 13, or the day strip contradicts the summary printed above it.
            SELECT exit_ts::date AS d, COUNT(*) AS n, SUM(pnl) AS pnl,
                   COUNT(*) FILTER (WHERE result = 'TARGET') AS wins,
                   COUNT(*) FILTER (WHERE result = 'SL') AS losses
            FROM v8_paper_trades
            WHERE exit_ts >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date - %(days)s
              AND (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND NOT (basket = ANY(%(retired)s))
            GROUP BY exit_ts::date
            ORDER BY d DESC
        """, {"cut": cutover, "days": days, "retired": _retired})
        dl = _rows(cur)
        # cc#970: the summary is THE CANON. This endpoint used to run its own COUNT/SUM and do
        # its own brokerage deduction — the trade-log-vs-master-dashboard mismatch the founder
        # reported on 09-Aug. It computes nothing now.
        canon = book_canon(conn, era=("all" if era == "all" else "fresh"))

    def f(v):
        return float(v) if v is not None else None

    return {
        "trades": [{
            "symbol": t["symbol"], "side": (t["side"] or "").upper(),
            "basket": basket_label(t["basket"]),
            "entry": f(t["entry_price"]), "exit": f(t["exit_price"]),
            "qty": f(t["qty"]), "pnl": f(t["pnl"]), "ret_pct": f(t["return_pct"]),
            "result": t["result"],
            "when": t["exit_ts"].strftime("%d %b %H:%M") if t["exit_ts"] else None,
        } for t in tr],
        "day_log": [{
            "date": d["d"].strftime("%Y-%m-%d"),
            "label": d["d"].strftime("%a %d %b"),
            "n": d["n"], "pnl": f(d["pnl"]),
            "wins": d["wins"], "losses": d["losses"],
        } for d in dl],
        "summary": {
            "era": canon["era"],
            "canon": canon["canon"],
            "retired_baskets": canon["retired_baskets"],
            "trades": canon["trades"],
            "gross": canon["gross"],
            "brokerage": canon["brokerage"],
            "realised": canon["realised"],
            "wins": canon["wins"], "losses": canon["losses"],
            "decided": canon["decided"], "win_rate": canon["win_rate"],
        },
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/api/mobile/v8lower")
@_json_safe
def mobile_v8_lower(request: Request, days: int = 30, top: int = 10):
    """cc#977 — the three lower modules of /m/v8 in ONE fetch.

    One call, because the three modules render together and three requests would let them disagree
    about "today" — the funnel is keyed to the newest signal_date and the segments to the newest
    v8_metrics score_date, and a slow second request could straddle a tick.

    EVERY P&L FIGURE IS THE CANON (rule 13). The day rows are the same v8_paper_trades rows the
    website daylog reads, under the canon scope (fresh era, retired baskets from the registry),
    with the day's net computed the canon way: SUM(pnl) minus Rs.500 per closed trade that day.
    The header total is `book_canon(...)['realised']` itself — not a sum of the day rows — so the
    module headline cannot drift from the tiles above it. The two AGREE by construction and the
    payload ships both so a mismatch would be visible rather than silent.

    A win is result='TARGET' and a loss is result='SL', here as everywhere else.
    """
    g = _guard(request)
    if g:
        return g
    days = max(1, min(days, 180))
    top = max(1, min(top, 25))
    now = _ist_now()
    with _conn() as conn, conn.cursor() as cur:
        canon = book_canon(conn, era="fresh")
        _retired, _ = retired_baskets(cur)
        cur.execute("SELECT value FROM app_config WHERE key='v8_paper_rebuild_cutover_ts'")
        _row = cur.fetchone()
        cutover = _row[0] if _row and _row[0] else None

        # ── 1 · day-wise net, canon scope, brokerage netted PER DAY ──────────────────────────
        cur.execute("""
            SELECT COALESCE(closed_at::date, exit_ts::date) AS d,
                   COUNT(*) AS n,
                   ROUND(SUM(pnl)::numeric, 2)                       AS gross,
                   COUNT(*) * 500                                    AS brokerage,
                   ROUND((SUM(pnl) - COUNT(*) * 500)::numeric, 2)    AS net,
                   COUNT(*) FILTER (WHERE result = 'TARGET')         AS wins,
                   COUNT(*) FILTER (WHERE result = 'SL')             AS losses
            FROM v8_paper_trades
            WHERE (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND NOT (basket = ANY(%(retired)s))
              AND COALESCE(closed_at, exit_ts) IS NOT NULL
            GROUP BY 1 ORDER BY 1 DESC
        """, {"cut": cutover, "retired": _retired})
        drows = _rows(cur)

        # ── 2 · funnel top-N per basket, newest signal_date ──────────────────────────────────
        # cc#985 FIX (my cc#977 regression): there is no `score` column on v8_qualified — the
        # whole endpoint 500'd, taking all THREE lower modules down, not just the funnel.
        # And gvm_score is NOT the right substitute either, which is why this reads filter_score:
        # the web funnel (/api/v8/qualified/{basket}) surfaces metrics->>'filter_score' out of
        # metrics->>'filter_total' as the QUALIFYING score — how many of that basket's own filter
        # rules the symbol passed. gvm_score is the 0-10 GVM quality rating, a different question.
        # Both ship: filter_score is the headline (it is what "qualified" means), gvm_score rides
        # along because it is the other number the founder reads on a name.
        cur.execute("""
            SELECT basket, symbol, gvm_score, cmp, signal_date,
                   (metrics->>'filter_score')::numeric AS filter_score,
                   (metrics->>'filter_total')::numeric AS filter_total
            FROM v8_qualified
            WHERE signal_date = (SELECT MAX(signal_date) FROM v8_qualified)
            ORDER BY basket,
                     (metrics->>'filter_score')::numeric DESC NULLS LAST,
                     gvm_score DESC NULLS LAST
        """)
        qrows = _rows(cur)

        # ── 3 · segments — cc#1042: THE APP DOES NOT GROUP ANYTHING. ──────────────────────────
        # It used to run its own GROUP BY on gvm_scores.segment over v8_metrics, which produced 55
        # groups against the website's 22 — and 18 of the 55 held exactly ONE stock, so tiles like
        # "Restaurants & QSR" and "Flexible Packaging" ranked as top sector movers off a single
        # name's day. Two surfaces, two taxonomies, and the phone was showing the wrong one.
        #
        # That query is DELETED, not parameterised. The panel now calls v8_theme_sectors() — the
        # same function behind /api/v8/theme_sectors that the web "All Futures" tab reads — so the
        # two surfaces are not "kept in sync", they are the same code. There is no app-side
        # grouping left that could drift. Same 22 themes, same equal-weight cc#338 maths, same
        # 3-member guard, same sort.
        #
        # Called in-process rather than over HTTP to itself, which is the pattern this endpoint
        # already uses for the NIFTY level (domestic_live, just below). It opens its own short
        # connection, so it is deliberately invoked AFTER this block's cursor work rather than
        # inside it — a failure there can only cost the segments card, never the ledger above it.

    _ts_basis, _ts_min, srows = None, None, []
    try:
        from v8_endpoints import v8_theme_sectors
        _ts = v8_theme_sectors()
        srows = [{"segment": s["segment"], "day_pct": s["avg_day_1d"],
                  "n": s["members_priced"], "members_total": s["members_total"],
                  "suppressed": s["suppressed"]} for s in (_ts.get("sectors") or [])]
        _ts_basis, _ts_min = _ts.get("basis"), _ts.get("min_members")
    except Exception as e:
        log.warning("v8lower: theme_sectors unavailable (%s)", e)

    # cc#985 SECOND BAD COLUMN, found by auditing the rest of this endpoint rather than stopping
    # at the reported one: global_indices has no `close` column (it is price/prev_close), AND it
    # carries no Nifty row at all — so this card could never have rendered even once the column
    # name was right. NIFTY50 does live in intraday_prices, but that index feed is the patchy one
    # flagged in cc#940/952 (today: 21 bars against BANKNIFTY's 43). So this reuses
    # domestic_live() — the same function mobile_home2 already uses for the Home ticker, which
    # falls back to raw_prices when the live leg is thin. One source for the index level across
    # the app, rather than a fourth private derivation.
    nifty = None
    try:
        from v8_endpoints import domestic_live
        _idx = (domestic_live() or {}).get("indices") or {}
        _n = _idx.get("NIFTY50") or {}
        if _n.get("close") is not None:
            nifty = {"close": _n.get("close"), "chg_pct": _n.get("chg_pct")}
    except Exception as e:
        log.warning("v8lower: domestic_live unavailable (%s)", e)

    def f(v):
        return float(v) if v is not None else None

    day_rows = [{
        "date": r["d"].isoformat(),
        "label": r["d"].strftime("%a %d %b"),
        "n": r["n"], "gross": f(r["gross"]), "brokerage": r["brokerage"],
        "net": f(r["net"]), "wins": r["wins"], "losses": r["losses"],
    } for r in drows]

    baskets = {}
    for r in qrows:
        baskets.setdefault(r["basket"], []).append({
            "symbol": r["symbol"],
            "score": f(r["filter_score"]),          # the qualifying score (rules passed)
            "score_total": f(r["filter_total"]),    # ...out of this basket's own rule count
            "gvm": f(r["gvm_score"]),
            "cmp": f(r["cmp"]),
        })
    funnel = [{"basket": b, "label": basket_label(b), "n": len(v), "rows": v[:top]}
              for b, v in sorted(baskets.items())]

    # cc#1042: members_total and suppressed ride along so the phone can print the denominator and
    # can tell "no reading today" apart from "flat" — a NULL rendered as 0.00% would be a lie.
    segs = [{"segment": r["segment"], "day_pct": f(r["day_pct"]), "n": r["n"],
             "members_total": r["members_total"], "suppressed": r["suppressed"]} for r in srows]

    return {
        # the module headline reads THIS, not a sum of day_rows — one number, one source (rule 13)
        "realised": canon["realised"],
        "canon": canon["canon"],
        "era": canon["era"],
        "retired_baskets": canon["retired_baskets"],
        "day_rows": day_rows,
        "day_count": len(day_rows),
        # shipped so a drift between the daily rows and the canon total is VISIBLE, not silent
        "day_sum": round(sum((d["net"] or 0.0) for d in day_rows), 2) if day_rows else None,
        "funnel": funnel,
        "signal_date": (qrows[0]["signal_date"].isoformat() if qrows else None),
        "segments": segs,
        "segment_count": len(segs),
        # cc#1042 item 1: the basis is quoted from the endpoint that computed it, never restated
        # here. If the two ever disagreed it would mean the app had started grouping again.
        "segment_basis": _ts_basis,
        "segment_taxonomy": "futures_universe.theme",
        "segment_min_members": _ts_min,
        "segment_suppressed": sum(1 for s in segs if s.get("suppressed")),
        "nifty": nifty,
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# cc#986 · PER-BASKET FILTER FUNNEL for /m/v8
#
# ROOT CAUSE FIRST. The founder's screenshot showed the basket expand rendering nothing at all.
# mobile/v8.html was calling /api/v8/funnel/{basket}, and that route had been bound to the WRONG
# FUNCTION since cc#424 on 12-Jul (06932bb): the decorator was left stranded above a helper that
# was inserted beneath it, so FastAPI read the helper's un-annotated `cur` argument as a required
# query string and answered 422 to every call. Fixed in v8_endpoints.py in this same commit. The
# funnel was never "unrecognised" — it was never delivered.
#
# ONE FUNNEL, NOT TWO (DISPLAY_PARITY 16202, and the card's first instruction). Everything here is
# the WEB's own computation, called IN-PROCESS: funnel_detail(basket) for the stage counts and
# stock_passcount(basket) for the per-stock gate results. No SQL of my own re-derives a gate. If a
# gate is added to the BASKET_FILTERS registry tomorrow it appears on mobile with no code change,
# exactly as it does on the web.
#
# The expand is served by the four per-basket detail endpoints the amendment named. There is NO
# generic /stock_detail/{basket} route — the map below is explicit for that reason.
_BASKET_DETAIL = {
    "buy_reversal":  "br_stock_detail",
    "sell_reversal": "sr_stock_detail",
    "sell_momentum": "sm_stock_detail",
    "buy_momentum":  "bm_stock_detail",
}


@router.get("/api/mobile/v8funnel")
@_json_safe
def mobile_v8_funnel(request: Request, basket: str = "", top: int = 5):
    """Stage counts + the top N symbols that pass EVERY gate but have no open position.

    `top` defaults to 5 (the card). A basket with fewer than N shows what exists and says so;
    a basket with none says that plainly — nothing is ever padded to reach five.
    """
    g = _guard(request)
    if g:
        return g
    top = max(1, min(top, 25))
    from v8_endpoints import funnel_detail, stock_passcount

    wanted = [b.strip().lower() for b in basket.split(",") if b.strip()] or list(_BASKET_DETAIL)
    wanted = [b for b in wanted if b in _BASKET_DETAIL]
    if not wanted:
        return {"error": "unknown basket", "known": list(_BASKET_DETAIL)}

    # OPEN means OPEN NOW, not "opened today". A position entered on Friday and still running has
    # very much been converted — listing its symbol as "passed but not opened" would be false. The
    # match is per (symbol, basket) because this is a per-basket funnel: the same name can be open
    # in a different basket and still be an un-taken opportunity in this one.
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT basket, symbol FROM v8_paper_positions WHERE status = 'OPEN'")
        open_pairs = set((r[0], r[1]) for r in cur.fetchall())

    out = []
    for b in wanted:
        item = {"basket": b, "label": basket_label(b), "stages": [], "rows": [],
                "detail_path": "/api/v8/" + _BASKET_DETAIL[b] + "/"}
        try:
            fd = funnel_detail(b) or {}
            item["stages"] = [{
                "key": s.get("key") or s.get("metric"),
                "metric": (s.get("metric") or "").replace("_", " "),
                "cond": (s.get("condition_min") or "") or (s.get("condition_max") or ""),
                "survivors": s.get("survivors"),
                "killed": s.get("killed"),
            } for s in (fd.get("stages") or [])]
            item["universe"] = fd.get("universe") or fd.get("total")
            # `final` is the strict-AND count the engine itself qualified — the bottom of the
            # funnel. It ships beside the stage rows so the reader can see the drop-off end to end.
            item["final"] = fd.get("final")
            item["as_of"] = str(fd.get("score_date") or "") or None
        except Exception as e:
            log.warning("v8funnel: funnel_detail(%s) failed (%s)", b, e)
            item["stages_error"] = f"{type(e).__name__}: {str(e)[:160]}"

        try:
            pc = stock_passcount(b) or {}
            stocks = pc.get("stocks") or []
            item["filter_count"] = pc.get("filter_count")
            # "passed every gate" is passed == total, taken from the web's own count — not a
            # threshold I picked. stock_passcount already returns them ranked (passed desc, then
            # gvm_score desc); that ORDER IS KEPT, so mobile ranks by what the web ranks by.
            full = [s for s in stocks if s.get("total") and s.get("passed") == s.get("total")]
            item["passing"] = len(full)
            not_open = [s for s in full if (b, s.get("symbol")) not in open_pairs]
            item["pass_not_open"] = len(not_open)
            item["open_now"] = len(full) - len(not_open)
            item["rows"] = [{
                "symbol": s.get("symbol"),
                "passed": s.get("passed"),
                "total": s.get("total"),
                "gvm": float(s["gvm_score"]) if s.get("gvm_score") is not None else None,
            } for s in not_open[:top]]
            item["truncated"] = max(0, len(not_open) - len(item["rows"]))
            if not full:
                item["message"] = "No symbol clears every gate right now."
            elif not not_open:
                item["message"] = ("All %d symbols that clear every gate are already open."
                                   % len(full))
        except Exception as e:
            log.warning("v8funnel: stock_passcount(%s) failed (%s)", b, e)
            item["rows_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        out.append(item)

    return {"baskets": out, "top": top,
            "as_of": _ist_now().strftime("%Y-%m-%d %H:%M:%S")}


@router.get("/m/screeners", response_class=HTMLResponse)
def m_screeners():
    return _page("screeners")


@router.get("/m/sector", response_class=HTMLResponse)
def m_sector():
    return _page("sector")


@router.get("/m/holdings", response_class=HTMLResponse)
def m_holdings():
    return _page("holdings")


@router.get("/m/fpc", response_class=HTMLResponse)
def m_fpc():
    return _page("fpc")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# cc#915 — THE V8 OPEN BOOK, position by position.
#
# WHY IT LIVES HERE: it is the ledger's sibling. /api/mobile/trades above is the CLOSED half of
# the same book at the same web-parity scope; putting the OPEN half anywhere else would be two
# owners for one book. Realised and win/loss are NOT recomputed here — the page reads them from
# /api/mobile/trades, which already computes them the web way (33 trades, gross 366,112, net
# 349,612, 13/5, verified against the web cards). One computation, one answer.
#
# THE EQUITY/FUTURES SWITCH — WHAT THE DATA ACTUALLY SAYS. The card asked for the split and told
# me to verify the columns before wiring it. I did, and there is nothing to split on:
# v8_paper_positions is (id, symbol, side, basket, entry_price, entry_ts, qty, target, stop_loss,
# pp, pivot_date, status) and v8_paper_trades carries no instrument field either. Neither has an
# account, segment or instrument column. Further, the book is priced on SPOT EQUITY by design —
# /api/paper/status pins CMP with the price_sources.py futures exclusion (cc#1056) so a futures bar can never
# land in the CMP column (cc#367). So the whole V8 paper book is one equity book. All 18 open
# rows are F&O-universe symbols, but that says the symbol HAS futures, not that the position IS
# one. A Futures tab would therefore be an empty box pretending to be a view, so this endpoint
# states instrument='equity' and futures_available=false, and the page shows the control with
# Futures disabled and the reason on it. Wiring it for real needs an instrument column written
# by the engine — an engine change, not a screen change.
# ══════════════════════════════════════════════════════════════════════════════════════════════

_NO_FUTURES_NOTE = ("V8 paper positions carry no instrument column and CMP is pinned to the spot "
                    "bar, so the whole book is equity. A futures leg would need the engine to "
                    "record one.")


@router.get("/api/mobile/v8book")
@_json_safe
def mobile_v8book(request: Request):
    """cc#915 — every OPEN V8 paper position with live CMP, day move and P&L.

    SAME SCOPE AS THE LEDGER AND THE HOME CARD: fresh era (entry_ts >= the cc#504 cutover) with
    retired baskets excluded via the registry. The three surfaces must agree — cc#970 makes that
    Open Book card to get here, and two different numbers for one book is the drift cc#894 was
    filed for.

    CMP IS THE WEB'S OWN LATERAL, copied not re-derived: latest intraday_prices close for the
    symbol with the price_sources.py futures exclusion (cc#367 — without that filter a futures bar
    at the same 5-min stamp lands in the CMP column at a basis-off price; cc#1056 widened it from
    the bare 'fyers_fut' literal to the registry, which also covers the cc#770 REST fallback leg). prev_close is the last raw_prices
    close strictly before the CMP's OWN session (cc#373), so DAY% compares two different days
    off-market instead of Friday against itself.

    entry_ts is NAIVE IST (verified: timestamp without time zone) — read raw, never converted.

    PROGRESS is computed here rather than in the template so the honesty rule lives in one place:
    it is the fraction travelled from stop_loss to target, normalised so 0 = stop and 1 = target
    for BOTH sides, and it is NULL whenever either level is missing or the two are equal. A
    position with no target never gets a drawn rail."""
    g = _guard(request)
    if g:
        return g
    now = _ist_now()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM app_config WHERE key='v8_paper_rebuild_cutover_ts'")
        _row = cur.fetchone()
        cutover = _row[0] if _row and _row[0] else None
        _open_retired, _ = retired_baskets(cur)         # cc#970: registry, same set as the canon
        cur.execute("""
            SELECT p.symbol, p.side, p.basket, p.entry_price, p.entry_ts, p.qty,
                   p.target, p.stop_loss, p.pivot_date,
                   COALESCE(lp.cmp, p.entry_price) AS cmp,
                   lp.ts AS cmp_ts,
                   pc.prev_close
            FROM v8_paper_positions p
            LEFT JOIN LATERAL (
                SELECT close AS cmp, ts FROM intraday_prices
                WHERE symbol = p.symbol AND """ + NOT_FUT_SQL + """
                ORDER BY ts DESC LIMIT 1
            ) lp ON true
            LEFT JOIN LATERAL (
                SELECT close AS prev_close FROM raw_prices
                WHERE symbol = p.symbol
                  AND price_date < COALESCE(lp.ts::date, (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
                ORDER BY price_date DESC LIMIT 1
            ) pc ON true
            WHERE p.status = 'OPEN'
              AND (%(cut)s::timestamp IS NULL OR p.entry_ts >= %(cut)s::timestamp)
              AND NOT (p.basket = ANY(%(retired)s))
            ORDER BY p.entry_ts DESC
        """, {"cut": cutover, "retired": _open_retired})
        rows = _rows(cur)

    def f(v):
        return float(v) if v is not None else None

    today = now.date()
    out, unrl_total, dep_total = [], 0.0, 0.0
    for r in rows:
        side = (r["side"] or "").upper()
        entry, cmp_, qty = f(r["entry_price"]), f(r["cmp"]), (r["qty"] or 0)
        tgt, stop, prev = f(r["target"]), f(r["stop_loss"]), f(r["prev_close"])
        sign = -1.0 if side == "SHORT" else 1.0
        unrl = round((cmp_ - entry) * qty * sign, 2) if (entry is not None and cmp_ is not None) else None
        deployed = round(entry * qty, 2) if entry is not None else None
        ret = (round((cmp_ - entry) / entry * 100.0 * sign, 2)
               if (entry not in (None, 0) and cmp_ is not None) else None)
        day = (round((cmp_ - prev) / prev * 100.0, 2)
               if (prev not in (None, 0) and cmp_ is not None) else None)

        # 0 = stop, 1 = target, same reading for a long and a short. None when it cannot be drawn.
        prog = None
        if tgt is not None and stop is not None and cmp_ is not None and tgt != stop:
            span = (tgt - stop) if side != "SHORT" else (stop - tgt)
            trav = (cmp_ - stop) if side != "SHORT" else (stop - cmp_)
            if span:
                prog = round(max(0.0, min(1.0, trav / span)), 4)

        ent_ts = r["entry_ts"]
        out.append({
            "symbol": r["symbol"], "side": side,
            "basket": r["basket"], "basket_label": basket_label(r["basket"]),
            "qty": qty, "entry_price": entry, "cmp": cmp_,
            "target": tgt, "stop_loss": stop,
            "unrealised": unrl, "return_pct": ret, "day_pct": day,
            "deployed": deployed, "progress": prog,
            "entry_ts": ent_ts.strftime("%Y-%m-%d %H:%M") if ent_ts else None,
            "entry_date": ent_ts.date().isoformat() if ent_ts else None,
            "entry_label": ent_ts.strftime("%d %b %H:%M") if ent_ts else None,
            "days_held": (today - ent_ts.date()).days if ent_ts else None,
            "cmp_at": r["cmp_ts"].strftime("%d %b %H:%M") if r["cmp_ts"] else None,
            "cmp_is_live": r["cmp_ts"] is not None,
        })
        if unrl is not None:
            unrl_total += unrl
        if deployed is not None:
            dep_total += deployed

    # ── cc#943 (and cc#942, the same card filed twice): THE REALISED PAGE ──────────────────────
    # The book is one thing with two faces. Both ship on THIS payload rather than a second
    # endpoint, so a swipe is instant and — more importantly — the two pages are guaranteed to be
    # the same book at the same instant. A separate fetch could land the open page on one scope
    # and the realised page on another, which is exactly the drift cc#894 was filed for.
    #
    # SCOPE IS THE CANON (cc#970, rule 13). This list still runs its own query because it needs
    # the ROWS, not just the totals — but it runs under the SAME scope the canon uses, taking the
    # retired set from the registry instead of naming 's1_reclaim_obs' in a string. Every SUMMARY
    # figure below now comes from book_canon() and is not computed here at all, so the numbers on
    # this page cannot drift from Home or the web master dashboard.
    # (Measured 09-Aug: s1_reclaim_obs has 0 closed rows and buy_s1_bounce's 10 rows are all
    # pre-cutover, so the registry exclusion changes no figure TODAY — it is there so that
    # retiring the next basket is one config row and no code, per rule 9.)
    #
    # SOURCE: v8_paper_trades, NOT v8_paper_positions. The positions table has no exit columns at
    # all — checked before building — so a closed V8 trade simply does not exist there.
    # entry_ts / exit_ts are NAIVE IST (timestamp without time zone), read raw, never converted.
    # CONTEXT ISOLATION (rule 7): v8_paper_* only. tc_intraday_* is not touched anywhere here.
    with _conn() as conn, conn.cursor() as cur:
        canon = book_canon(conn, era="fresh")           # cc#970: every summary figure, one source
        _retired, _ = retired_baskets(cur)
        cur.execute("""
            SELECT id, symbol, side, basket, entry_price, entry_ts, exit_price, exit_ts, qty,
                   target, stop_loss, pnl, return_pct, result
            FROM v8_paper_trades
            WHERE (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND NOT (basket = ANY(%(retired)s))
            ORDER BY exit_ts DESC NULLS LAST, id DESC
        """, {"cut": cutover, "retired": _retired})
        crows = _rows(cur)

    # cc#970: real_total / real_dep / the per-side counters are gone — those sums are the canon's
    # job now. `wins`/`losses` survive ONLY as a drift check against it (see below).
    closed, wins, losses = [], 0, 0
    for r in crows:
        side = (r["side"] or "").upper()
        entry, ex, qty = f(r["entry_price"]), f(r["exit_price"]), (r["qty"] or 0)
        pnl = f(r["pnl"])
        ret = f(r["return_pct"])
        if ret is None and entry not in (None, 0) and ex is not None:
            ret = round((ex - entry) / entry * 100.0 * (-1.0 if side == "SHORT" else 1.0), 2)
        dep = round(entry * qty, 2) if entry is not None else None
        ets, xts = r["entry_ts"], r["exit_ts"]
        closed.append({
            # id is negated so a closed row can never collide with an OPEN position's id in the
            # template's single expanded-card key. Two pages, one key space, no cross-page ghost.
            "id": -(r["id"] or 0), "trade_id": r["id"], "symbol": r["symbol"], "side": side,
            "basket": r["basket"], "basket_label": basket_label(r["basket"]),
            "qty": qty, "entry_price": entry, "exit_price": ex,
            "target": f(r["target"]), "stop_loss": f(r["stop_loss"]),
            # `realised` deliberately mirrors the open page's `unrealised` key so the shared card
            # renderer reads one field name on both pages instead of branching on which page it is.
            "realised": pnl, "return_pct": ret, "deployed": dep,
            "result": (r["result"] or "").upper() or None,
            "entry_ts": ets.strftime("%Y-%m-%d %H:%M") if ets else None,
            "entry_label": ets.strftime("%d %b %H:%M") if ets else None,
            "exit_ts": xts.strftime("%Y-%m-%d %H:%M") if xts else None,
            "exit_date": xts.date().isoformat() if xts else None,
            "exit_label": xts.strftime("%d %b %H:%M") if xts else None,
            "days_held": ((xts.date() - ets.date()).days if (ets and xts) else None),
        })
        # cc#950 PARITY FIX — W/L are the WEB MASTER DASHBOARD counts: result='TARGET' vs 'SL'.
        # cc#943 (mine) counted a win as pnl > 0, which is a DIFFERENT question and gives a
        # different answer: on the same 33 fresh-era trades, pnl>0 says 21W/12L = 63.6% while
        # TARGET/SL says 13W/5L = 72.2%. Fifteen trades closed for some other reason — real money
        # in the realised figure, but not a verdict, and calling them losses because they ended
        # flat-to-down invents a record the book never had. Home already used the canonical
        # counts, so the two surfaces were contradicting each other about one book.
        _res = (r["result"] or "").upper()
        if _res == "TARGET":
            wins += 1
        elif _res == "SL":
            losses += 1

    # cc#970: NO SUMMARY MATHS HERE ANY MORE. real_brokerage, the net realised, the win rate and
    # the per-side records were all computed locally in this function; every one of them is now
    # taken from book_canon(). The loop above still walks the rows, but only to BUILD THE LIST —
    # its wins/losses counters are kept solely as a self-check (asserted against the canon below),
    # not as a second source of truth.
    _local_w, _local_l = wins, losses
    if (_local_w, _local_l) != (canon["wins"], canon["losses"]):
        # scope drift between the list query and the canon: say so rather than render either.
        log.warning("V8_PNL_CANON drift on /api/mobile/v8book: list says %sW/%sL, canon says %sW/%sL",
                    _local_w, _local_l, canon["wins"], canon["losses"])

    return {
        "instrument": "equity",
        "futures_available": False,
        "instrument_note": _NO_FUTURES_NOTE,
        "open": len(out),
        "positions": out,
        # cc#979: the OPEN-book totals are the canon's too. cc#970 migrated the realised figures
        # and left these three computing locally from the row loop — a rule-13 gap I left behind.
        # They agree with the canon today (same scope, same arithmetic), but "agrees today" is not
        # the contract: one formula, one place. The per-row `deployed` above stays local because
        # it is a property of the row, not a book total.
        "unrealised": canon["unrealised"],
        "deployed": canon["deployed"],
        "unrealised_pct": canon["unrealised_pct"],
        "long": canon["long"],
        "short": canon["short"],
        # shipped so a divergence between the listed rows and the canon is VISIBLE, not silent
        "rows_unrealised": round(unrl_total, 2) if out else None,
        "rows_deployed": round(dep_total, 2) if out else None,
        # cc#970: every figure below comes from the canon. The cc#950 parity fix (realised NET of
        # brokerage, wins = TARGET, losses = SL) is not repeated here — it now lives in exactly one
        # place, v8_book_canon, and this page consumes it like every other surface.
        "closed_count": len(closed),
        # cc#976: the canon's own `trades` key ships too. closed_count is the length of the LIST
        # this endpoint returns; `trades` is the canon's closed-trade count. They agree today, but
        # the stat row must read the canon key so /m/v8 and Home cannot diverge if the list is ever
        # capped or paged.
        "trades": canon["trades"],
        "closed": closed,
        "realised": canon["realised"],
        "gross": canon["gross"],
        "brokerage": canon["brokerage"],
        "realised_deployed": canon["realised_deployed"],
        "realised_pct": canon["realised_pct"],
        "wins": canon["wins"], "losses": canon["losses"], "decided": canon["decided"],
        # A rate with nothing decided is a DASH, not 0%: no verdicts is not a 0% record.
        "win_rate": canon["win_rate"],
        "rec_long": canon["rec_long"],
        "rec_short": canon["rec_short"],
        "era": canon["era"],
        "canon": canon["canon"],
        "retired_baskets": canon["retired_baskets"],
        "scope_note": ("Both pages: fresh era (entry on/after the cc#504 cutover), retired "
                       "baskets excluded via the app_config registry. One formula, V8_PNL_CANON_V1."),
        "rail": rail_state(now, 5, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
