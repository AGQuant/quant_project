"""
result_corner.py — cc#602 RESULT CORNER coverage backfill + calendar-vs-news verify.
================================================================================
Per RESULT_CORNER_SPEC_V1 (session_log 7135) part2+part3. The NSE/screener earnings feed misses
companies (esp. small/midcaps); the polished_news result-announcement stream catches them. This
module DISCOVERS reported companies from polished_news (already alias-matched into
mentioned_symbols by news_tagger), RECONCILES them into earnings_calendar (status='reported'), and
runs a recurring VERIFY that alerts when the news-reported set leads the stored calendar — so the
reported base stays complete automatically going forward.

DISCOVERY: polished_news headline/summary result-announcement match (results / earnings / net profit
/ PAT / board-meeting-outcome / QxFYxx) within a window; symbols from mentioned_symbols (news_tagger
alias-matching), with a news_tagger._match fallback for rows the polisher left untagged.

RECONCILE (idempotent): a discovered symbol that resolves to a real nse_code and is NOT already
status='reported' in earnings_calendar for the current quarter gets a reported row upserted on the
(ticker, ex_date) unique key — event_type='Quarterly Result', status='reported', verified=false
(news-sourced, not NSE-confirmed), reschedule_log noting the source. It is ALSO enqueued into
ops_metrics_t1_queue so the existing T+1/Saturday chain stages its doc-text + re-scrapes fundamentals.

VERIFY (recurring, read-only alert): compares the news-result symbol count vs the stored
reported count for the quarter; a news-leads-by-threshold gap writes an ops_log alert
(RESULT_CORNER_VERIFY) — the Engine-Watchdog signal (standing rule 7130) — then applies a bounded
reconcile. Registered as bg_result_corner_verify (scheduler_master, ~08:15 IST daily).

Web-search cross-check (spec's secondary discovery source) is Claude-web's on-demand verification
(web egress); this module owns the polished_news path, which is the spec's PRIMARY discovery.
"""
import logging
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException
from typing import Dict, Optional, Tuple

log = logging.getLogger("scorr.result_corner")
router = APIRouter(prefix="/api/admin/result_corner", tags=["result_corner"])
page_router = APIRouter(tags=["result_corner_page"])   # cc#603: public page API at /api/result-corner
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# result-announcement detectors (kept precise — these gate writes into the live earnings feed)
_HEADLINE_RX = (r"(Q[1-4]\s?FY2[0-9]|quarterly result|net profit|profit after tax|"
                r"reports? .*(profit|loss)|board meeting|consolidated net|earnings|"
                r"PAT (rose|fell|up|down|jump|decline)|revenue (rose|grew|up))")
_SUMMARY_RX = (r"(net profit|profit after tax|Q[1-4]\s?FY2[0-9]|quarterly results|"
               r"reported .*(profit|loss)|board .*approved .*results)")
DISCOVERY_DAYS = 25     # rolling window for the polished_news scan (one reporting season's tail)
NEWS_LEAD_ALERT = 8     # news-leads-calendar gap that trips the watchdog alert


def _conn():
    import fyers_feed
    return fyers_feed.get_db()


def _oplog(cur, title, details, category="result_corner"):
    import json
    cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                   VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                (category, title, json.dumps(details, default=str)))


def _quarter_start() -> date:
    """Start of the currently-reporting quarter window (~last completed quarter-end minus a small
    lead) — used to scope 'already reported this quarter'. Mirrors the last-quarter cadence: a report
    filed now is for the just-closed quarter, so a 30-day back-window catches the season."""
    return date.today() - timedelta(days=35)


def discover_reported(conn, days: int = DISCOVERY_DAYS) -> Dict[str, dict]:
    """Scan polished_news for result-announcement items in the window; return
    {SYMBOL: {"ex_date": date, "headline": str}} using the LATEST result item per symbol. Symbols
    come from mentioned_symbols (news_tagger alias-matched); untagged rows fall back to _match."""
    out: Dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.mentioned_symbols, p.headline_clean, p.summary, p.published_time
            FROM polished_news p
            WHERE p.published_time > NOW() - (%s || ' days')::interval
              AND (p.headline_clean ~* %s OR p.summary ~* %s)
            ORDER BY p.published_time ASC
        """, (str(days), _HEADLINE_RX, _SUMMARY_RX))
        rows = cur.fetchall()

    index = None   # lazily built news_tagger index for the fallback
    for syms, headline, summary, published in rows:
        pub_date = published.date() if published else date.today()
        matched = [str(s).upper().strip() for s in (syms or []) if s]
        if not matched:
            # fallback: alias-match the headline+summary via news_tagger (rows the polisher missed)
            try:
                import news_tagger
                if index is None:
                    index = news_tagger.build_index(conn)
                text = f"{headline or ''} {summary or ''}"
                matched = news_tagger._match(text.lower(), text.upper(), index)
            except Exception as e:
                log.warning(f"result_corner _match fallback failed: {e}")
                matched = []
        for sym in matched:
            # ASC order => the last write per symbol is its latest result item
            out[sym] = {"ex_date": pub_date, "headline": (headline or "")[:180]}
    return out


def _resolve(cur, sym: str) -> Optional[str]:
    """Return the company_name for a symbol if it is a real nse_code, else None (drops index/junk
    tags that made it into mentioned_symbols)."""
    cur.execute("SELECT company_name FROM screener_raw WHERE UPPER(nse_code)=%s LIMIT 1", (sym,))
    r = cur.fetchone()
    return r[0] if r else None


def reconcile(conn, days: int = DISCOVERY_DAYS, apply: bool = True) -> dict:
    """Add news-discovered reported companies missing from earnings_calendar. Idempotent: upsert on
    (ticker, ex_date); skip symbols already status='reported' this quarter. Each add is also enqueued
    into ops_metrics_t1_queue so the T+1/Saturday chain stages its docs + re-scrapes fundamentals."""
    discovered = discover_reported(conn, days)
    q_start = _quarter_start()
    added = updated = skipped_present = unresolved = enqueued = 0
    samples = []
    with conn.cursor() as cur:
        cur.execute("""SELECT UPPER(ticker) FROM earnings_calendar
                       WHERE status='reported' AND ex_date >= %s""", (q_start,))
        already = {r[0] for r in cur.fetchall()}
        for sym, info in discovered.items():
            if sym in already:
                skipped_present += 1
                continue
            company = _resolve(cur, sym)
            if not company:
                unresolved += 1
                continue
            if not apply:
                added += 1
                continue
            ex_date = info["ex_date"]
            cur.execute("""
                INSERT INTO earnings_calendar
                    (company_name, ticker, ex_date, event_type, status, verified, first_seen,
                     last_updated, reschedule_log)
                VALUES (%s,%s,%s,'Quarterly Result','reported',FALSE,NOW(),NOW(),
                        jsonb_build_array(jsonb_build_object('src','cc#602','ts',NOW()::text,'note',%s)))
                ON CONFLICT (ticker, ex_date) DO UPDATE SET
                    status='reported', last_updated=NOW(),
                    reschedule_log=COALESCE(earnings_calendar.reschedule_log,'[]'::jsonb)
                        || jsonb_build_array(jsonb_build_object('src','cc#602','ts',NOW()::text,
                                                                'note','news-confirmed reported'))
                RETURNING (xmax=0) AS inserted
            """, (company, sym, ex_date, f"news-discovered ({info['headline']})"))
            inserted = cur.fetchone()[0]
            if inserted:
                added += 1
            else:
                updated += 1
            # feed the existing T+1/Saturday pipeline (stage docs + re-scrape fundamentals)
            cur.execute("""INSERT INTO ops_metrics_t1_queue (symbol, ex_date, status)
                           VALUES (%s,%s,'pending') ON CONFLICT (symbol, ex_date) DO NOTHING""",
                        (sym, ex_date))
            enqueued += 1
            if len(samples) < 20:
                samples.append(f"{sym}:{ex_date}")
        if apply:
            _oplog(cur, "RESULT_CORNER_RECONCILE",
                   {"discovered": len(discovered), "added": added, "updated": updated,
                    "skipped_already_reported": skipped_present, "unresolved": unresolved,
                    "enqueued_t1": enqueued, "window_days": days, "sample": samples})
        conn.commit()
    return {"discovered": len(discovered), "added": added, "updated": updated,
            "skipped_already_reported": skipped_present, "unresolved": unresolved,
            "enqueued_t1": enqueued, "sample": samples}


def verify(conn) -> dict:
    """Read-only: compare the news-result symbol count vs the stored reported count for the quarter.
    A news-leads-calendar gap beyond NEWS_LEAD_ALERT trips a watchdog alert (rule 7130)."""
    discovered = discover_reported(conn)
    q_start = _quarter_start()
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(DISTINCT UPPER(ticker)) FROM earnings_calendar
                       WHERE status='reported' AND ex_date >= %s""", (q_start,))
        cal_reported = cur.fetchone()[0]
        # discovered symbols that resolve AND are missing from the calendar = the true lead
        missing = 0
        for sym in discovered:
            cur.execute("""SELECT 1 FROM earnings_calendar WHERE UPPER(ticker)=%s
                           AND status='reported' AND ex_date >= %s LIMIT 1""", (sym, q_start))
            if cur.fetchone() is None and _resolve(cur, sym):
                missing += 1
        gap = missing
        alert = gap >= NEWS_LEAD_ALERT
        if alert:
            _oplog(cur, "RESULT_CORNER_VERIFY",
                   {"event": "news_leads_calendar", "news_result_syms": len(discovered),
                    "calendar_reported": cal_reported, "missing_from_calendar": gap,
                    "threshold": NEWS_LEAD_ALERT,
                    "note": "polished_news reports results the earnings_calendar has not captured"},
                   category="result_corner")
        conn.commit()
    return {"news_result_syms": len(discovered), "calendar_reported": cal_reported,
            "missing_from_calendar": gap, "alert": alert}


def run_daily_verify(conn=None) -> dict:
    """Recurring (bg_result_corner_verify): verify the news-vs-calendar gap (alert if news leads),
    then apply a bounded reconcile so the reported base stays complete automatically."""
    own = conn is None
    conn = conn or _conn()
    try:
        v = verify(conn)
        r = reconcile(conn, apply=True)
        # cc#618 Section B AUTO-WIRE: newly-reconciled announced names go STRAIGHT into result_analysis
        # regeneration (canonical build_card) — the verify job detected them, now it also regenerates,
        # so no manual batches. build_card upgrades to the latest quarter as fundamentals land (the
        # cc#596 T+1 chain calls the same fn). Best-effort — a regen failure never fails the verify.
        ra = None
        try:
            import result_analysis_gen
            ra = result_analysis_gen.regenerate(conn)
        except Exception as e:
            log.warning(f"result_corner result_analysis regen wire: {e}")
        return {"verify": v, "reconcile": r, "result_analysis": ra}
    finally:
        if own:
            conn.close()


def run_backfill(conn=None, days: int = 40) -> dict:
    """One-time comprehensive backfill: reconcile over a wider (season) window so ALL announced
    results to date land in earnings_calendar. After this, the T+1 pipeline + run_daily_verify keep
    it complete automatically."""
    own = conn is None
    conn = conn or _conn()
    try:
        return reconcile(conn, days=days, apply=True)
    finally:
        if own:
            conn.close()


# ── endpoints ─────────────────────────────────────────────────────────────────────────────────────
def _check_admin(tok):
    if not ADMIN_TOKEN or tok != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="admin token required")


@router.post("/backfill")
def backfill_now(days: int = 40, x_admin_token: Optional[str] = Header(None)):
    """cc#602 one-time comprehensive backfill of all news-announced results to date."""
    _check_admin(x_admin_token)
    return run_backfill(days=days)


@router.get("/preview")
def preview(days: int = DISCOVERY_DAYS):
    """Dry-run: what the reconcile WOULD add (no writes). Safe to hit any time."""
    conn = _conn()
    try:
        return reconcile(conn, days=days, apply=False)
    finally:
        conn.close()


@router.get("/status")
def status():
    conn = _conn()
    try:
        return verify(conn)
    finally:
        conn.close()


# ── cc#603: Result Corner page API (tier-filtered, server-paginated reported companies) ────────────
def _num(v):
    """fundamentals_history metric values are strings like '34,309' / '11%' / '-16'."""
    if v is None:
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _pct(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return round((cur - prev) / abs(prev) * 100.0, 1)


def _snapshot(cur, symbol):
    """Latest reported quarter Sales & Net Profit + QoQ (vs prev q) + YoY (vs 4q ago), from
    fundamentals_history section='quarters' (consolidated preferred)."""
    cur.execute("""SELECT period_label, period_end, metrics, consolidated FROM fundamentals_history
                   WHERE symbol=%s AND section='quarters' AND period_type='quarter'
                   ORDER BY period_end DESC NULLS LAST LIMIT 6""", (symbol,))
    rows = cur.fetchall()
    if not rows:
        return {}
    has_cons = any(r[3] for r in rows)
    rows = [r for r in rows if bool(r[3]) == has_cons]
    if not rows:
        return {}
    q = [{"label": r[0], "sales": _num((r[2] or {}).get("Sales")),
          "pat": _num((r[2] or {}).get("Net Profit"))} for r in rows]
    latest, prev, yoy = q[0], (q[1] if len(q) > 1 else None), (q[4] if len(q) > 4 else None)
    return {
        "quarter": latest["label"], "sales": latest["sales"], "net_profit": latest["pat"],
        "sales_qoq": _pct(latest["sales"], prev["sales"]) if prev else None,
        "sales_yoy": _pct(latest["sales"], yoy["sales"]) if yoy else None,
        "pat_qoq": _pct(latest["pat"], prev["pat"]) if prev else None,
        "pat_yoy": _pct(latest["pat"], yoy["pat"]) if yoy else None,
    }


def _fq_label(period_end):
    """'Q1 FY27' for a quarter period-end (Jun->Q1, Sep->Q2, Dec->Q3, Mar->Q4)."""
    if not period_end:
        return None
    m, y = period_end.month, period_end.year
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(m)
    if q is None:
        return None
    fy = (y + 1) if m >= 4 else y
    return f"Q{q} FY{str(fy)[-2:]}"


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0, 1)


@page_router.get("/api/result-corner/v2")
def result_corner_v2():
    """cc#628 RESULT CORNER V2 — one payload driving all three sections. HONESTY DOCTRINE (cc#625
    fix_3): every aggregate is over SAME-QUARTER reporters only (a company whose latest fundamentals
    quarter == the season quarter), and coverage counts (n/total) are always returned. season quarter
    = the modal latest fundamentals quarter across the scored universe (the quarter the bulk are on).
    Zero new feeds: gvm_scores + fundamentals_history + sector_ratings + earnings_calendar."""
    from collections import Counter, defaultdict
    conn = _conn()
    try:
        with conn.cursor() as cur:
            # 1) scored universe -> tier (AMFI mcap rank) + segment + GVM
            cur.execute("""SELECT symbol, company_name, segment, market_cap, gvm_score, verdict,
                                  ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) mrank
                           FROM gvm_scores WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)""")
            uni = {}
            for sym, co, seg, mc, g, v, mr in cur.fetchall():
                tier = "Large" if mr <= 100 else "Mid" if mr <= 250 else "Small"
                uni[sym] = {"company": co, "segment": seg or "Unclassified",
                            "gvm": float(g) if g is not None else None, "verdict": v, "tier": tier}
            # 2) bulk recent quarters, grouped per symbol (consolidated-preferred, newest first)
            cur.execute("""SELECT symbol, period_end, metrics, consolidated, period_label
                           FROM fundamentals_history
                           WHERE section='quarters' AND period_type='quarter' AND period_end IS NOT NULL
                             AND period_end >= (CURRENT_DATE - INTERVAL '520 days')
                           ORDER BY symbol, period_end DESC""")
            rowsby = defaultdict(list)
            for sym, pe, metrics, cons, plabel in cur.fetchall():
                rowsby[sym].append((pe, metrics or {}, cons, plabel))
            fund = {}
            for sym, rows in rowsby.items():
                has_cons = any(r[2] for r in rows)
                rws = [r for r in rows if bool(r[2]) == has_cons] or rows

                def _rev(md):
                    s = _num(md.get("Sales"))
                    return s if s is not None else _num(md.get("Revenue"))
                q = [{"pe": r[0], "sales": _rev(r[1]), "pat": _num(r[1].get("Net Profit"))} for r in rws]
                latest = q[0]; prev = q[1] if len(q) > 1 else None; yoy = q[4] if len(q) > 4 else None
                fund[sym] = {"latest_q": latest["pe"], "sales": latest["sales"], "pat": latest["pat"],
                             "sales_qoq": _pct(latest["sales"], prev["sales"]) if prev else None,
                             "sales_yoy": _pct(latest["sales"], yoy["sales"]) if yoy else None,
                             "pat_qoq": _pct(latest["pat"], prev["pat"]) if prev else None,
                             "pat_yoy": _pct(latest["pat"], yoy["pat"]) if yoy else None}
            # 3) season quarter = the LIVE season = the NEWEST quarter-end anyone has reported (the one
            # currently being declared), NOT the modal/last-fully-scraped quarter. Same-quarter reporters
            # are those who have already filed it; everyone else shows a dash + coverage grows through the
            # season (honesty doctrine — "90 of 1600 have declared Q1 so far", not last quarter restated).
            if not fund:
                return {"season": None, "summary": {}, "sectors": [], "companies": []}
            season_end = max(f["latest_q"] for f in fund.values())
            same = [s for s in fund if fund[s]["latest_q"] == season_end and s in uni]
            # 4) sector GVM ratings + reported dates
            cur.execute("SELECT segment, ROUND(mcap_weighted_gvm::numeric,2), verdict FROM sector_ratings")
            secrt = {r[0]: {"rating": float(r[1]) if r[1] is not None else None, "verdict": r[2]} for r in cur.fetchall()}
            cur.execute("""SELECT UPPER(ticker), MAX(ex_date) FROM earnings_calendar
                           WHERE status='reported' GROUP BY UPPER(ticker)""")
            repdate = {r[0]: r[1] for r in cur.fetchall()}

        # ── per-segment same-quarter medians (needed for beats-vs-sector) ──
        seg_syms = defaultdict(list)
        for s in same:
            seg_syms[uni[s]["segment"]].append(s)
        seg_pat_median = {}
        sectors = []
        seg_total = Counter(uni[s]["segment"] for s in uni)
        for seg, syms in seg_syms.items():
            sy = [fund[s]["sales_yoy"] for s in syms]
            py = [fund[s]["pat_yoy"] for s in syms]
            pat_med = _median(py)
            seg_pat_median[seg] = pat_med
            pos = sum(1 for s in syms if (fund[s]["pat_yoy"] or 0) > 0)
            pat_vals = [s for s in syms if fund[s]["pat_yoy"] is not None]
            sectors.append({
                "sector": seg, "reported": len(syms), "total": seg_total.get(seg, len(syms)),
                "sales_yoy": _median(sy), "pat_yoy": pat_med,
                "pct_positive": round(pos / len(pat_vals) * 100) if pat_vals else None,
                "gvm": (secrt.get(seg) or {}).get("rating"),
                "gvm_verdict": (secrt.get(seg) or {}).get("verdict"),
            })
        sectors.sort(key=lambda x: (x["pat_yoy"] is None, -(x["pat_yoy"] or 0)))

        # ── section 01 summary (same-quarter only) ──
        def _band(x):
            return "flat" if (x is None or abs(x) < 0.5) else ("pos" if x > 0 else "neg")
        pos = sum(1 for s in same if _band(fund[s]["pat_yoy"]) == "pos")
        neg = sum(1 for s in same if _band(fund[s]["pat_yoy"]) == "neg")
        flat = len(same) - pos - neg
        beats = sum(1 for s in same if fund[s]["pat_yoy"] is not None
                    and seg_pat_median.get(uni[s]["segment"]) is not None
                    and fund[s]["pat_yoy"] > seg_pat_median[uni[s]["segment"]])
        tier_split = {}
        for t in ("Large", "Mid", "Small"):
            tot = sum(1 for s in uni if uni[s]["tier"] == t)
            rep = sum(1 for s in same if uni[s]["tier"] == t)
            tier_split[t] = {"reported": rep, "total": tot}
        summary = {
            "reported": len(same), "total": len(uni),
            "pct": round(len(same) / len(uni) * 100) if uni else 0,
            "pat_growing": pos, "pat_flat": flat, "pat_declining": neg, "beats_sector": beats,
            "median_sales_yoy": _median([fund[s]["sales_yoy"] for s in same]),
            "median_pat_yoy": _median([fund[s]["pat_yoy"] for s in same]),
            "median_sales_qoq": _median([fund[s]["sales_qoq"] for s in same]),
            "median_pat_qoq": _median([fund[s]["pat_qoq"] for s in same]),
            "tiers": tier_split,
        }

        # ── section 03 companies: same-quarter reporters (numbers) + reported-but-unscraped (dashes) ──
        season_win = date.today() - timedelta(days=45)
        listed = set(same) | {s for s, d in repdate.items() if s in uni and d and d >= season_win}
        companies = []
        for s in listed:
            u = uni[s]; f = fund.get(s); is_same = s in fund and fund[s]["latest_q"] == season_end
            companies.append({
                "symbol": s, "company": u["company"], "segment": u["segment"], "tier": u["tier"],
                "gvm": u["gvm"], "verdict": u["verdict"],
                "reported_date": str(repdate.get(s)) if repdate.get(s) else None,
                # HONESTY: figures ONLY for same-quarter reporters; else dash (quarter not in data yet)
                "sales": f["sales"] if is_same else None, "pat": f["pat"] if is_same else None,
                "sales_qoq": f["sales_qoq"] if is_same else None, "sales_yoy": f["sales_yoy"] if is_same else None,
                "pat_qoq": f["pat_qoq"] if is_same else None, "pat_yoy": f["pat_yoy"] if is_same else None,
            })
        companies.sort(key=lambda c: (c["reported_date"] is None, c["reported_date"] or "", ), reverse=True)
        return {"season": {"quarter": _fq_label(season_end), "quarter_end": str(season_end)},
                "summary": summary, "sectors": sectors, "companies": companies}
    finally:
        conn.close()


@page_router.get("/api/result-corner")
def result_corner_list(tier: str = "all", page: int = 1, per_page: int = 30):
    """cc#603: reported companies (newest first), server-paginated, with mcap tier (AMFI rank by
    market_cap DESC: Large 1-100 / Mid 101-250 / Small 251+), GVM verdict, and a result snapshot.
    tier = large | mid | small | all. mcap_tier is computed on-the-fly from gvm_scores.market_cap."""
    tier = (tier or "all").lower()
    page = max(1, int(page)); per_page = min(100, max(1, int(per_page)))
    conn = _conn()
    try:
        with conn.cursor() as cur:
            # rank the whole scored universe by market cap -> tier; join the reported set
            cur.execute("""
                WITH ranked AS (
                    SELECT symbol, market_cap, gvm_score, verdict, segment,
                           ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS mrank
                    FROM gvm_scores WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)
                ),
                tiered AS (
                    SELECT *, CASE WHEN mrank<=100 THEN 'large' WHEN mrank<=250 THEN 'mid' ELSE 'small' END AS tier
                    FROM ranked
                ),
                reported AS (
                    SELECT UPPER(ticker) AS sym, MAX(ex_date) AS reported_date, MAX(company_name) AS company
                    FROM earnings_calendar WHERE status='reported'
                    GROUP BY UPPER(ticker)
                )
                SELECT r.sym, r.company, r.reported_date, t.tier, t.gvm_score, t.verdict, t.mrank
                FROM reported r LEFT JOIN tiered t ON t.symbol=r.sym
                WHERE (%s='all' OR t.tier=%s)
                ORDER BY r.reported_date DESC NULLS LAST, t.mrank ASC NULLS LAST
            """, (tier, tier))
            allrows = cur.fetchall()
            total = len(allrows)
            start = (page - 1) * per_page
            pagerows = allrows[start:start + per_page]
            out = []
            for sym, company, rdate, t, gscore, verdict, mrank in pagerows:
                snap = _snapshot(cur, sym)
                out.append({
                    "symbol": sym, "company": company, "reported_date": str(rdate) if rdate else None,
                    "tier": t, "mcap_rank": mrank, "gvm_score": float(gscore) if gscore is not None else None,
                    "gvm_verdict": verdict, "snapshot": snap,
                    "result_card_url": f"/check?symbol={sym}",
                })
        return {"tier": tier, "page": page, "per_page": per_page, "total": total,
                "pages": (total + per_page - 1) // per_page, "results": out}
    finally:
        conn.close()
