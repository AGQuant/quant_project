"""
digest_v3.py — cc#846 DAILY DIGEST V3.

Pixel reference: design_refs/scorr_digest_v3_R2.html (commit 120a80b) — a DESIGN reference with a
hardcoded 04-Aug 06:45 IST snapshot. This module supplies the live bindings behind those pixels.

BINDING SPEC: session_log id=965 (daily_digest_format_v2 v2.3). V3 is a PRESENTATION upgrade, not a
re-spec, so every section 965 defines still ships. The reference consolidates ten sections into
seven panels; nothing is dropped. The mapping, stated once so a future reader can audit it:

    965 §1  Global Indices      -> 01 Global Tape        (two-level DAY/WEEK, cc#842 component)
    965 §2  Domestic Indices    -> 02 Index Ladders      (CMP row inside the ladder)
    965 §3  Support Levels      -> 02 Index Ladders      (S1/S2 rungs)
    965 §4  Pivot Points        -> 02 Index Ladders      (PP/R1/R2 rungs)
    965 §5  PCR Trend           -> 03 Internals          (with ADR breadth)
    965 §6  Top Domestic News   -> 05 What Moved
    965 §7  Events/Results Today-> 04 Reporting Today
    965 §8  Results Yesterday   -> 07 Yesterday's Results · Scored   (the V3 upgrade)
    965 §9  Global Events       -> 06 Global Events
    965 §10 Market Read         -> 08 Market Read        (kept as its own closing section)

HARD CONSTRAINTS (cc#846): READ-ONLY. No new tables, no new fetch jobs, no new scheduler entries.

THE RULE THIS MODULE EXISTS TO ENFORCE — RESULT-STORY MATCHING.
Matching polished_news to a ticker on mentioned_symbols ALONE produces WRONG CONTENT. Verified
against the live DB while building this: KEI had 1 loose match (a POLYCAB headline), JSL 2, UPL 2,
and ATHERENERG 7 — the loose rule would have attached a confident, wrong story to each. All three
conditions are therefore required together: ticker in mentioned_symbols AND a result-shaped headline
AND published within 20 hours. Under the strict rule those four drop to 0 matches and render an
explicit PENDING pill. They are NEVER dropped from the table — dropping silently misrepresents a
"top 10 by market cap" list, which is the one thing that list promises.

SECTION READS are computed from the numbers already on the page (965 MARKET_READ_FRAMEWORK:
"every claim tied to a number already shown above"). They are deterministic, not model-generated —
consistent with the $0/native doctrine and with "no new fetch jobs".
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

import psycopg2
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.digest_v3")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

INDEXES = ["NIFTY50", "BANKNIFTY"]

# cc#846: the five global symbols with no intraday feed. Named explicitly because the spec requires
# them called out by name in the amber EOD note — "stale but unflagged" is the bug being fixed.
EOD_ONLY = ["000001.SS", "^FTSE", "^GDAXI", "INDIAVIX", "^VIX"]

RESULT_KEYWORDS = r"(profit|loss|revenue|income|Q1|results|rises|narrows|falls)"
RESULT_WINDOW_HOURS = 20


def _conn():
    return psycopg2.connect(_DB)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct(a, b) -> Optional[float]:
    a, b = _f(a), _f(b)
    return round((a / b - 1) * 100, 2) if (a is not None and b) else None


def _ist_now() -> datetime:
    return datetime.now(IST)


def market_state(now: datetime) -> Dict[str, Any]:
    """PRE-OPEN (with countdown) / OPEN / CAS / CLOSED — computed from the clock, never assumed."""
    t = now.time()
    wd = now.weekday() < 5
    if not wd:
        return {"state": "CLOSED", "label": "CLOSED · WEEKEND", "mins_to_open": None}
    if t < datetime.strptime("09:15", "%H:%M").time():
        target = now.replace(hour=9, minute=15, second=0, microsecond=0)
        mins = int((target - now).total_seconds() // 60)
        return {"state": "PRE-OPEN", "label": f"PRE-OPEN · {mins//60}h {mins%60}m TO BELL",
                "mins_to_open": mins}
    if t < datetime.strptime("15:15", "%H:%M").time():
        return {"state": "OPEN", "label": "OPEN", "mins_to_open": 0}
    if t < datetime.strptime("15:30", "%H:%M").time():
        return {"state": "CAS", "label": "CAS · CLOSING AUCTION", "mins_to_open": 0}
    return {"state": "CLOSED", "label": "CLOSED", "mins_to_open": None}


# ── 01 GLOBAL TAPE — reuse the cc#842 component, never a second implementation ────────────────
def _global_tape(cur) -> Dict[str, Any]:
    try:
        import global_heatstrip
        strip = global_heatstrip.build_strip(cur)
    except Exception as e:
        log.warning("cc#846 global tape unavailable: %s", e)
        return {"tiles": [], "eod_tiles": [], "error": str(e)[:200], "tier": "EOD"}
    strip["tier"] = "LIVE"
    strip["eod_note"] = ("No intraday feed for " + ", ".join(EOD_ONLY) +
                         " — these carry their last CLOSE, shown separately with their own date.")
    tiles = (strip.get("tiles") or [])
    ups = sum(1 for t in tiles if (t.get("day_chg_pct") or 0) > 0)
    strip["read"] = (f"{ups} of {len(tiles)} live tiles higher on the day."
                     if tiles else "No live global tiles available.")
    return strip


# ── 02 INDEX LADDERS ──────────────────────────────────────────────────────────────────────────
def _ladders(cur) -> Dict[str, Any]:
    out = []
    for sym in INDEXES:
        cur.execute("""SELECT pp, r1, r2, s1, s2, pivot_date FROM v8_paper_pivots
                       WHERE symbol=%s ORDER BY pivot_date DESC LIMIT 1""", (sym,))
        p = cur.fetchone()
        cur.execute("""SELECT close, price_date FROM raw_prices WHERE symbol=%s AND close>0
                       ORDER BY price_date DESC LIMIT 1""", (sym,))
        c = cur.fetchone()
        cmp_v = _f(c[0]) if c else None
        as_of, tier = (str(c[1]) if c else None), "EOD"
        # Live CMP during the session, from the same intraday table every other surface reads.
        cur.execute("""SELECT close, ts FROM intraday_prices
                       WHERE symbol=%s AND close IS NOT NULL ORDER BY ts DESC LIMIT 1""", (sym,))
        iv = cur.fetchone()
        if iv and iv[0] is not None:
            cmp_v, as_of, tier = _f(iv[0]), str(iv[1]), "LIVE"
        if not p:
            out.append({"symbol": sym, "cmp": cmp_v, "as_of": as_of, "tier": tier,
                        "rungs": [], "reason": "no stored pivots"})
            continue
        pp, r1, r2, s1, s2, pdate = (_f(p[0]), _f(p[1]), _f(p[2]), _f(p[3]), _f(p[4]), p[5])
        rungs = [("R2", r2), ("R1", r1), ("PP", pp), ("S1", s1), ("S2", s2)]
        # Distance % on every rung, and the CMP's true position between them — the ladder must show
        # WHERE price sits, not just list levels.
        band = [{"label": k, "value": v,
                 "dist_pct": (round((v / cmp_v - 1) * 100, 2) if (v and cmp_v) else None)}
                for k, v in rungs if v is not None]
        above = [b for b in band if b["value"] > (cmp_v or 0)]
        out.append({"symbol": sym, "cmp": cmp_v, "as_of": as_of, "tier": tier,
                    "pivot_date": str(pdate) if pdate else None,
                    "rungs": band, "cmp_slot": len(above)})
    reads = []
    for l in out:
        if l.get("cmp") and l.get("rungs"):
            pp = next((r for r in l["rungs"] if r["label"] == "PP"), None)
            if pp and pp["dist_pct"] is not None:
                side = "above" if l["cmp"] > pp["value"] else "below"
                reads.append(f"{l['symbol']} {side} PP by {abs(pp['dist_pct']):.2f}%")
    return {"indexes": out, "tier": "LIVE", "read": ("; ".join(reads) if reads else
            "No pivot ladder available — v8_paper_pivots has no recent row for these indexes.")}


# ── 03 INTERNALS: ADR breadth + PCR ───────────────────────────────────────────────────────────
def _internals(cur) -> Dict[str, Any]:
    cur.execute("""SELECT price_date, advances, declines, unchanged, adr FROM adr_daily
                   ORDER BY price_date DESC LIMIT 1""")
    a = cur.fetchone()
    cur.execute("""SELECT price_date, underlying, pcr, quality, quality_note FROM pcr_daily
                   WHERE price_date=(SELECT MAX(price_date) FROM pcr_daily) ORDER BY underlying""")
    pcr_rows = [{"date": str(r[0]), "underlying": r[1], "pcr": _f(r[2]),
                 "quality": r[3], "note": r[4]} for r in cur.fetchall()]
    cur.execute("""SELECT price_date, underlying, pcr FROM pcr_daily
                   WHERE price_date >= (SELECT MAX(price_date) FROM pcr_daily) - 10
                   ORDER BY price_date DESC, underlying""")
    trend = [{"date": str(r[0]), "underlying": r[1], "pcr": _f(r[2])} for r in cur.fetchall()]

    adr = {"date": str(a[0]), "advances": a[1], "declines": a[2],
           "unchanged": a[3], "adr": _f(a[4])} if a else None

    # 965 stale_flag_rule: >2 trading days old gets flagged, never shown bare.
    def _stale(dstr):
        if not dstr:
            return False
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
            return (_ist_now().date() - d).days > 4      # >2 trading days, weekend-tolerant
        except Exception:
            return False

    bits = []
    if adr and adr["adr"] is not None:
        bits.append(f"breadth {adr['adr']:.2f} ({adr['advances']}A/{adr['declines']}D)")
    for r in pcr_rows:
        if r["pcr"] is not None:
            who = "put writers" if r["pcr"] > 1 else "call writers"
            bits.append(f"{r['underlying']} PCR {r['pcr']:.2f} — {who} in control")
    return {"adr": adr, "adr_stale": _stale(adr["date"]) if adr else False,
            "pcr": pcr_rows, "pcr_stale": _stale(pcr_rows[0]["date"]) if pcr_rows else False,
            "pcr_trend": trend, "tier": "LIVE",
            "read": ("; ".join(bits) if bits else "No breadth or options data available.")}


# ── 04 REPORTING TODAY / 05 WHAT MOVED / 06 GLOBAL EVENTS ─────────────────────────────────────
def _reporting_today(cur) -> Dict[str, Any]:
    cur.execute("""SELECT UPPER(e.ticker), e.company_name, s.market_cap
                   FROM earnings_calendar e
                   LEFT JOIN screener_raw s ON s.nse_code = UPPER(e.ticker)
                   WHERE e.ex_date = CURRENT_DATE
                   ORDER BY s.market_cap DESC NULLS LAST LIMIT 25""")
    rows = [{"ticker": r[0], "company": r[1], "market_cap": _f(r[2])} for r in cur.fetchall()]
    return {"companies": rows, "count": len(rows), "tier": "STATIC",
            "read": (f"{len(rows)} companies report today."
                     if rows else "No scheduled reporters today.")}


def _news(cur, category: str, limit: int = 6) -> List[Dict[str, Any]]:
    cur.execute("""SELECT headline_clean, COALESCE(summary, full_summary), source, published_time
                   FROM polished_news WHERE category=%s
                   ORDER BY published_time DESC LIMIT %s""", (category, limit))
    return [{"headline": r[0], "summary": r[1], "source": r[2],
             "published": r[3].isoformat() if r[3] else None} for r in cur.fetchall()]


# ── 07 YESTERDAY'S RESULTS · SCORED — the strict three-condition match ────────────────────────
_BULL = r"(rises|jumps|surges|beats|narrows|record|expands|grows|up \d)"
_BEAR = r"(falls|drops|slumps|misses|widens|declines|loss|down \d)"


def _yesterday_results(cur) -> Dict[str, Any]:
    cur.execute("""SELECT UPPER(e.ticker), e.company_name, s.market_cap
                   FROM earnings_calendar e
                   LEFT JOIN screener_raw s ON s.nse_code = UPPER(e.ticker)
                   WHERE e.ex_date = CURRENT_DATE - 1
                   ORDER BY s.market_cap DESC NULLS LAST LIMIT 10""", ())
    top = [{"ticker": r[0], "company": r[1], "market_cap": _f(r[2])} for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) FROM earnings_calendar WHERE ex_date = CURRENT_DATE - 1")
    total = cur.fetchone()[0]

    out, tally = [], {"bullish": 0, "cautious": 0, "neutral": 0, "pending": 0}
    for c in top:
        # ALL THREE conditions, together. See the module docstring for the verified failures that
        # any looser rule produces.
        cur.execute("""SELECT headline_clean, COALESCE(summary, full_summary), published_time
                       FROM polished_news
                       WHERE %s = ANY(mentioned_symbols)
                         AND headline_clean ~* %s
                         AND published_time >= NOW() - make_interval(hours => %s)
                       ORDER BY published_time DESC LIMIT 1""",
                    (c["ticker"], RESULT_KEYWORDS, RESULT_WINDOW_HOURS))
        n = cur.fetchone()
        if not n:
            tally["pending"] += 1
            out.append({**c, "headline": None, "summary": None, "read": "PENDING",
                        "pending_reason": "Reported — summary not yet on the desk"})
            continue
        head = n[0] or ""
        import re as _re
        bull = bool(_re.search(_BULL, head, _re.I))
        bear = bool(_re.search(_BEAR, head, _re.I))
        read = "BULLISH" if (bull and not bear) else ("CAUTIOUS" if bear else "NEUTRAL")
        tally["bullish" if read == "BULLISH" else "cautious" if read == "CAUTIOUS" else "neutral"] += 1
        out.append({**c, "headline": head, "summary": n[1], "read": read,
                    "published": n[2].isoformat() if n[2] else None})

    cur.execute("SELECT COUNT(DISTINCT symbol) FROM result_analysis_v2")
    l2 = cur.fetchone()[0]
    return {"companies": out, "reported_total": total, "tally": tally, "l2_count": l2,
            "tier": "STATIC",
            "match_rule": ("ticker in mentioned_symbols AND result-shaped headline AND published "
                           f"within {RESULT_WINDOW_HOURS}h — all three, or the row shows PENDING"),
            "read": (f"{tally['bullish']} bullish · {tally['cautious']} cautious · "
                     f"{tally['neutral']} neutral · {tally['pending']} awaiting desk")}


def build_digest(cur) -> Dict[str, Any]:
    now = _ist_now()
    tape = _global_tape(cur)
    lad = _ladders(cur)
    internals = _internals(cur)

    # 965 §10 MARKET READ — composed only from numbers already shown above, no new data.
    bias_bits = []
    tiles = tape.get("tiles") or []
    if tiles:
        ups = sum(1 for t in tiles if (t.get("day_chg_pct") or 0) > 0)
        bias_bits.append(f"global tape {ups}/{len(tiles)} higher")
    if internals.get("adr") and internals["adr"].get("adr") is not None:
        bias_bits.append(f"breadth {internals['adr']['adr']:.2f}")
    bias = "Range"
    if internals.get("adr") and (internals["adr"].get("adr") or 0) > 1.2:
        bias = "Bullish continuation"
    elif internals.get("adr") and 0 < (internals["adr"].get("adr") or 0) < 0.8:
        bias = "Cautious"

    return {
        "generated_at": now.isoformat(),
        "date_ist": now.strftime("%d %b %Y"),
        "market": market_state(now),
        "sections": {
            "global_tape": tape,
            "ladders": lad,
            "internals": internals,
            "reporting_today": _reporting_today(cur),
            "what_moved": {"items": _news(cur, "Domestic"), "tier": "LIVE"},
            "global_events": {"items": _news(cur, "Global"), "tier": "LIVE"},
            "yesterday_results": _yesterday_results(cur),
        },
        "market_read": {
            "bias": bias,
            "support": bias_bits,
            "note": "Composed only from the numbers shown above (965 MARKET_READ_FRAMEWORK).",
        },
        "spec": "cc#846 V3 · binding content spec session_log 965 v2.3",
    }


@router.get("/digest", response_class=HTMLResponse)
def digest_page():
    """cc#846: the V3 page. Read-only render; all data comes from /api/digest/v3."""
    import pathlib
    return HTMLResponse(pathlib.Path("scorr_digest_v3.html").read_text(encoding="utf-8"))


@router.get("/api/digest/v3")
def digest_v3():
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_digest(cur)
    except Exception as e:
        log.exception("digest_v3 failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "sections": {}}
