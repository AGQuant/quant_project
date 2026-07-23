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
        return {"verify": v, "reconcile": r}
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
