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

from price_sources import not_fut   # cc#1053 INDEX_SYMBOL_CONVENTION_V1
from typing import Dict, Any, List, Optional

import psycopg2
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.digest_v3")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

INDEXES = ["NIFTY50", "BANKNIFTY"]

# cc#852: the EOD set is no longer a hardcoded list. Shanghai is dropped, US VIX is retired from
# the tape, and India VIX ticks live off the NSE feed — so the old five-symbol constant named three
# symbols that either no longer exist on the tape or are not EOD at all. The tier now comes from
# global_heatstrip.build_strip(), which derives it per tile from whether a tick actually arrived,
# and the amber note is built from THAT result rather than from a constant that has to be
# remembered. A hardcoded list is exactly what went stale here.

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
    # cc#852: the note is built from the ACTUAL eod_tiles the component returned, naming the
    # symbols that really have no tick right now. It is omitted entirely when the set is empty —
    # after this card that is the normal case during Asia/Europe hours, and an empty amber banner
    # saying "no intraday feed for " would be worse than no banner.
    _eod = strip.get("eod_tiles") or []
    strip["eod_note"] = (
        "No tick yet for " + ", ".join((t.get("name") or t.get("symbol")) for t in _eod) +
        " — these carry their last CLOSE with its own date. A market outside its own hours is "
        "CLOSED, not stale."
    ) if _eod else None
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
        # cc#1053: cash leg only. The rungs below come from v8_paper_pivots, which are cash
        # pivots — so a BANKNIFTY futures print landing here measured the distance from a
        # cash S1/R1 to a price ~200 pts away on a different instrument. BANKNIFTY carries
        # both legs under one symbol (price_sources.py); NIFTY50 was safe by accident.
        cur.execute("""SELECT close, ts FROM intraday_prices
                       WHERE symbol=%s AND close IS NOT NULL
                         AND COALESCE(source,'') <> ALL(%s)
                       ORDER BY ts DESC LIMIT 1""", (sym, not_fut()))
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


# ── cc#1096 R7-D3 · A BARE BSE CODE IS NOT A COMPANY ──────────────────────────────────────────
# Founder recording, frame 3:12: REPORTING TODAY read 540027 · 521137 · TVVISION · ANNAPURNA ·
# BAGFILMS. Frame 3:20: YESTERDAY'S RESULTS carried 512379, 531515, 539175 among real tickers.
#
# THE RESOLUTION PATH, since the card asks for it by name: earnings_calendar.ticker is rendered
# straight to screen. That column holds a BSE scrip code for BSE-only companies, and the payload
# LEFT JOINs screener_raw on nse_code, which cannot match one — so the join fails silently, the
# market cap comes back NULL, and the raw code reaches the chip.
#
# I CHECKED WHETHER THEY COULD BE RESOLVED PROPERLY FIRST, because a real symbol always beats a
# name. screener_raw carries a "BSE Code" column, so the mapping looked possible. Measured:
#   earnings_calendar rows                                   2,470
#   rows whose ticker is all digits                            163
#   of those 163, resolvable to an nse_code via "BSE Code"       0
# Zero, because screener_raw IS the NSE-listed universe (1,816 rows) and these companies are not
# on the NSE at all. There is no NSE symbol to resolve to, and inventing one is forbidden.
#
# SO THE NAME IT IS, and the evidence supports it without a gap: company_name is populated on
# 2,470 of 2,470 rows — zero nulls. Every BSE-only reporter therefore has something honest to
# show. The ticker is KEPT in the payload untouched, because the news match in
# _yesterday_results keys on it against mentioned_symbols and that must not change.
_BSE_CODE_RE = None


def _label(ticker: str, company: str):
    """(display_label, is_symbol) — never a bare numeric code where a ticker belongs."""
    global _BSE_CODE_RE
    if _BSE_CODE_RE is None:
        import re as _re
        _BSE_CODE_RE = _re.compile(r"^\d+$")
    t = (ticker or "").strip()
    if t and not _BSE_CODE_RE.match(t):
        return t, True
    name = (company or "").strip()
    # If a numeric code somehow arrives with no name, show the code rather than an em-dash — the
    # reader can at least look it up, and a blank would hide that a company reported at all.
    return (name or t or "—"), False


# ── 04 REPORTING TODAY / 05 WHAT MOVED / 06 GLOBAL EVENTS ─────────────────────────────────────
def _reporting_today(cur) -> Dict[str, Any]:
    cur.execute("""SELECT UPPER(e.ticker), e.company_name, s.market_cap
                   FROM earnings_calendar e
                   LEFT JOIN screener_raw s ON s.nse_code = UPPER(e.ticker)
                   WHERE e.ex_date = CURRENT_DATE
                   ORDER BY s.market_cap DESC NULLS LAST LIMIT 25""")
    rows = []
    for r in cur.fetchall():
        lbl, is_sym = _label(r[0], r[1])
        rows.append({"ticker": r[0], "company": r[1], "market_cap": _f(r[2]),
                     "label": lbl, "is_symbol": is_sym})
    return {"companies": rows, "count": len(rows), "tier": "STATIC",
            "read": (f"{len(rows)} companies report today."
                     if rows else "No scheduled reporters today.")}


def _news(cur, category: str, limit: int = 6) -> List[Dict[str, Any]]:
    # cc#853: sentiment added so the R3 news rows can carry their .sdot colour and .nsent label.
    # It is READ from polished_news, never inferred here — the column is populated but its
    # vocabulary is inconsistent (Bullish/Positive/positive, Bearish/Negative/negative, Cautious,
    # Neutral/neutral), so it is passed through raw and normalised once, on the client.
    cur.execute("""SELECT headline_clean, COALESCE(summary, full_summary), source, published_time,
                          sentiment
                   FROM polished_news WHERE category=%s
                   ORDER BY published_time DESC LIMIT %s""", (category, limit))
    return [{"headline": r[0], "summary": r[1], "source": r[2],
             "published": r[3].isoformat() if r[3] else None,
             "sentiment": r[4]} for r in cur.fetchall()]


# ── 07 YESTERDAY'S RESULTS · SCORED — the strict three-condition match ────────────────────────
_BULL = r"(rises|jumps|surges|beats|narrows|record|expands|grows|up \d)"
_BEAR = r"(falls|drops|slumps|misses|widens|declines|loss|down \d)"


def _prev_trading_date(cur, ref=None):
    """cc#1109: the previous TRADING day, not CURRENT_DATE - 1.

    `CURRENT_DATE - 1` scores Sunday every Monday and scores a holiday on the day after one —
    both return an empty earnings set that reads as "nothing reported" rather than "no session".
    Walk back from `ref` (IST today by default) over weekends and over notified NSE closures.
    The holiday set comes from the nse_holidays TABLE, so a calendar update needs no deploy;
    weekends are pure date arithmetic and need no lookup at all.

    Bounded at 10 steps. The longest real NSE gap is a Diwali/weekend cluster of about four
    days, so 10 is slack, not a guess — and a bound means a bad calendar row can never spin.
    """
    d = ref or _ist_now().date()
    for _ in range(10):
        d = d - timedelta(days=1)
        if d.weekday() >= 5:                      # Sat/Sun
            continue
        cur.execute("SELECT 1 FROM nse_holidays WHERE holiday_date = %s LIMIT 1", (d,))
        if cur.fetchone():
            continue
        return d
    return (ref or _ist_now().date()) - timedelta(days=1)


def _yesterday_results(cur, basis) -> Dict[str, Any]:
    cur.execute("""SELECT UPPER(e.ticker), e.company_name, s.market_cap
                   FROM earnings_calendar e
                   LEFT JOIN screener_raw s ON s.nse_code = UPPER(e.ticker)
                   WHERE e.ex_date = %s
                   ORDER BY s.market_cap DESC NULLS LAST LIMIT 10""", (basis,))
    top = []
    for r in cur.fetchall():
        lbl, is_sym = _label(r[0], r[1])
        top.append({"ticker": r[0], "company": r[1], "market_cap": _f(r[2]),
                    "label": lbl, "is_symbol": is_sym})
    cur.execute("SELECT COUNT(*) FROM earnings_calendar WHERE ex_date = %s", (basis,))
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
            # cc#1109: the page states which session this scored, so an empty Monday reads as
            # "Friday had none" and never as "today had none".
            "basis_date": basis.isoformat(),
            "match_rule": ("ticker in mentioned_symbols AND result-shaped headline AND published "
                           f"within {RESULT_WINDOW_HOURS}h — all three, or the row shows PENDING"),
            "read": (f"{tally['bullish']} bullish · {tally['cautious']} cautious · "
                     f"{tally['neutral']} neutral · {tally['pending']} awaiting desk")}


def build_digest(cur) -> Dict[str, Any]:
    now = _ist_now()
    prev_trading = _prev_trading_date(cur, now.date())   # cc#1109: one basis, used everywhere
    tape = _global_tape(cur)
    lad = _ladders(cur)
    internals = _internals(cur)

    # 965 §10 MARKET READ. The SUPPORT numbers are still composed only from what is already on
    # the page — that half of the framework is unchanged.
    bias_bits = []
    tiles = tape.get("tiles") or []
    if tiles:
        ups = sum(1 for t in tiles if (t.get("day_chg_pct") or 0) > 0)
        bias_bits.append(f"global tape {ups}/{len(tiles)} higher")
    if internals.get("adr") and internals["adr"].get("adr") is not None:
        bias_bits.append(f"breadth {internals['adr']['adr']:.2f}")

    # cc#1109 VERDICT UNIFICATION — founder R7 defect 4. The bias WORD is now SOURCED, not
    # composed here. This module used to derive its own word from ADR alone while Home read
    # v8_endpoints.market_mood(); two composers produce two words for the same minute by
    # construction, and no amount of tuning either one fixes that. 965's MARKET_READ rule is
    # amended by this card: support numbers stay local, the word comes from the one composer.
    bias, bias_source, bias_note = None, "market_mood", None
    try:
        from v8_endpoints import market_mood as _market_mood
        bias = (_market_mood() or {}).get("mood") or None
    except Exception as e:                       # noqa: BLE001 — any failure falls back, none raises
        log.warning("cc#1109 market_mood unavailable, falling back to local ADR rule: %s", e)
        bias = None
    if not bias:
        # The fallback is the OLD rule, kept verbatim, and it says so on the page. A silent
        # fallback would put the two words back out of step with nothing on screen to show it.
        adr = (internals.get("adr") or {}).get("adr") or 0
        bias = "Range"
        if adr > 1.2:
            bias = "Bullish continuation"
        elif 0 < adr < 0.8:
            bias = "Cautious"
        bias_source = "digest_local_adr_fallback"
        bias_note = ("Market mood unavailable — this word is composed locally from breadth "
                     "alone and may differ from Home.")

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
            "yesterday_results": _yesterday_results(cur, prev_trading),
        },
        "prev_trading_date": prev_trading.isoformat(),   # cc#1109: for the page's date filters
        "market_read": {
            "bias": bias,
            "bias_source": bias_source,
            "support": bias_bits,
            "note": bias_note or ("Bias word from the shared market mood; support numbers "
                                  "composed only from what is shown above "
                                  "(965 MARKET_READ_FRAMEWORK, amended by cc#1109)."),
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
