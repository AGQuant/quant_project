"""
Trade Check v4 — DUAL-STYLE engine (canonical spec session_log id=2926, locked 11-Jul-2026).

WHY a new module (not an edit of tc_v4_endpoints.py): that file is the OLDER v4 (Tier1/Tier2,
single-direction, spec id=959) and stays LIVE and untouched. This is the founder's new dual-style
rulebook: every check runs BOTH a MOMENTUM and a REVERSAL card per side; the higher score wins and
carries the style label. Single tier = 3 hard gates + 15 scored rules (credit 1 / 0.5 / 0).

SHARED-MODULE CONTRACT (cc#386 + cc#387): all scoring works on a plain dict `d`. `_derive(d)` does
every derived-metric computation; the single-symbol loader (`_load_one`) and the batch loader
(`_load_bulk`, in tc_v4_scan) both fill the SAME raw fields then call the SAME `_derive` + the SAME
`score_card`. That is the "scanner score == single-symbol score, exactly" guarantee — one rulebook,
never duplicated.

Verdict bands (cc#767 V8-ALIGNMENT, spec id=12289, locked 30-Jul-2026): the score denominator is
DYNAMIC (propagation map id=265) — flow rules consolidated (R5+R6 -> Volume confirm, R12+R13 ->
Derivatives confirm, R14 ATR dropped) and the freed points reallocated to per-bucket live-V8 basket
gate imports, so each style bucket carries its own max. cc#934: that max is now SUMMED from each
rule's declared max rather than assumed as len(rules)+2 (which silently believed R18/R19 were the only
2-point rules — untrue once Volume became 2 on BUY-REV). cc#935: no card's max is written down here
any more — `card_maxes()` derives all four off the live rulebook and every surface reads it from
there, so a comment can no longer go stale against the code. Change a rule's weight and the
denominator, the bands and every footer follow on their own.
Bands re-baseline PROPORTIONALLY off each card's own max at the founder-locked ratios STRONG 0.84x
/ VALID 0.65x (from the prior BUY 18.5-22 / 14.4-22). Per-bucket imports: BUY-MOM +dma_50[5,12]
+w52>=75 +GVM>=7 (R7 twr bound [70,85]); BUY-REV R7->mRSI[60,90] +GVM>=6.5, R4 graded d200&d20;
SELL-MOM R11->CMP<PP +S2-clearance>=3%; SELL-REV R4 graded 3-DMA<0, R10 mom_2d[-4,-1]+w52<50.
Gates (cc#677, founder-final spec id=9035): ZERO VETO. The verdict is SCORE BANDS ALONE. The old
G1 GVM / G2 earnings / G3 futures-DTE gates AND the fo_ban check no longer reject anything — every
risk condition is now an informational ALERT CHIP (F&O ban · result-date · GVM floor · DTE countdown).

Endpoints (new paths — do NOT collide with the live /api/trade-check/v4):
  GET/POST /api/trade-check/v4/dual   — single symbol, both cards both sides (or a chosen side)
  GET      /api/trade-check/v4/health-dual
The batch scanner /api/trade-check/v4/scan lives in tc_v4_scan.py (cc#387) and imports this module.
"""

import os
from datetime import datetime, timedelta, date

import psycopg
from fastapi import APIRouter
from pydantic import BaseModel

from nifty_dwm import live_nifty_dwm
from r6_volume import volume_ratio
# reuse the pure low-level helpers from the older v4 module — no rule logic imported
from tc_v4_endpoints import _f, _r, _rsi, _weekly_closes, _current_expiry

router = APIRouter()

_DB = os.getenv("DATABASE_URL", "")

VERSION = "v4-dual.3-basket-aligned"
SPEC_REF = ("session_log id=2926 (v4 dual) + id=3010 (SELL recalibration, locked 12-Jul-2026) + "
            "cc#513 (18-Jul-2026, shared-field alignment to locked cc#502 basket specs)")

STYLES = ("MOMENTUM", "REVERSAL")
SIDES = ("BUY", "SELL")


def _ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _fmt_event_date(iso):
    """cc#451: render an imminent result date as 'DD-Mon (relative)' — e.g. '13-Jul (tomorrow)',
    '14-Jul (Tue)', 'today' — relative to IST today, for the G2 gate chip/banner."""
    if not iso:
        return None
    try:
        dt = date.fromisoformat(iso)
    except Exception:
        return str(iso)
    delta = (dt - _ist().date()).days
    rel = "today" if delta == 0 else "tomorrow" if delta == 1 else dt.strftime("%a")
    return f"{dt.strftime('%d-%b')} ({rel})"


# ── credit helpers ──────────────────────────────────────────────────────────────

def _both(a, b):
    """both true -> 1.0, one true -> 0.5, neither -> 0.0 (None counts as false)."""
    n = (1 if a else 0) + (1 if b else 0)
    return 1.0 if n == 2 else (0.5 if n == 1 else 0.0)


def _band(x, hi, mid_lo):
    """3-tier ascending band: x>=hi ->1, mid_lo<=x<hi ->0.5, else 0. None -> 0."""
    if x is None:
        return 0.0
    if x >= hi:
        return 1.0
    if x >= mid_lo:
        return 0.5
    return 0.0


def _R(rid, label, credit, value, required=None, max_credit=1.0):
    """cc#513: `required` is the exact style/side-aware condition string for the 4-column rule
    table (RULE | REQUIRED | ACTUAL | POINTS). Always built from the same threshold constants the
    credit branch above it just used -- never a hand-typed duplicate that can drift from the logic.

    cc#934: every rule now declares its OWN max_credit, and score_card sums them. The denominator
    used to be `len(rules) + 2`, which silently hard-coded the belief that R18 and R19 are the only
    2-point rules on every card. That was already fragile and cc#934 breaks it outright by making
    Volume worth 2 on BUY-REV. Deriving the max from the rules that actually fired means the
    denominator can never again disagree with the rulebook."""
    return {"rule": rid, "label": label, "credit": round(float(credit), 2),
            "max": round(float(max_credit), 2), "value": value, "required": required}


# cc#410 (session_log id=3019) — Fib Strength Rule zones (position in swing range: 0=low, 100=high).
_FIB_PCTS_16 = [0.0, 23.6, 38.2, 50.0, 61.8, 78.6, 100.0, 123.6]


def _fib_zone_name(pos):
    if pos is None:
        return None
    if pos >= 100:
        return "Breakout"
    if pos >= 78.6:
        return "Resistance Test"
    if pos >= 61.8:
        return "Strength"
    if pos >= 38.2:
        return "Decision"
    if pos >= 23.6:
        return "Weak"
    return "Breakdown"


def _fib_levels(hi, lo):
    """Standard fib ladder prices for a swing [lo, hi] (incl the 123.6 extension)."""
    if hi is None or lo is None or hi <= lo:
        return []
    span = hi - lo
    return [{"pct": p, "price": round(lo + (p / 100.0) * span, 2)} for p in _FIB_PCTS_16]


# ── derived-metric math (SHARED by single + bulk loaders) ────────────────────────

def _true_range_series(daily):
    """Wilder TR per day from daily OHLC rows (ascending)."""
    tr = []
    for i in range(1, len(daily)):
        h, l = daily[i]["high"], daily[i]["low"]
        pc = daily[i - 1]["close"]
        if None in (h, l, pc):
            continue
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def _derive(d):
    """Compute every derived metric the rules need. Pure function of the raw fields already on d —
    identical for single-symbol and batch so scores match exactly."""
    daily = d.get("daily") or []
    closes = [r["close"] for r in daily if r["close"] is not None]
    v8 = d.get("v8") or {}
    cmp_v = d.get("cmp")

    # TRUE calendar-weekly RSI-14 (basket-local convention) from weekly closes
    d["true_weekly_rsi"] = _rsi(_weekly_closes(daily)) if daily else None

    # ATR14 (simple mean of last 14 completed-day TRs) + today's TR + ignition flag (R14)
    tr = _true_range_series(daily)
    atr14 = sum(tr[-14:]) / len(tr[-14:]) if len(tr) >= 14 else (sum(tr) / len(tr) if tr else None)
    d["atr14"] = atr14
    bars = d.get("bars") or []
    day_hi = max([b["high"] for b in bars if b["high"] is not None], default=None) if bars else None
    day_lo = min([b["low"] for b in bars if b["low"] is not None], default=None) if bars else None
    prev_close = daily[-2]["close"] if len(daily) >= 2 else (daily[-1]["close"] if daily else None)
    if day_hi is not None and day_lo is not None and prev_close is not None:
        tr_today = max(day_hi - day_lo, abs(day_hi - prev_close), abs(day_lo - prev_close))
    elif len(daily) >= 1 and daily[-1]["high"] is not None:
        r0 = daily[-1]
        tr_today = (max(r0["high"] - r0["low"], abs(r0["high"] - prev_close), abs(r0["low"] - prev_close))
                    if prev_close is not None else (r0["high"] - r0["low"]))
    else:
        tr_today = None
    d["tr_today"] = tr_today
    d["ignition"] = (tr_today is not None and atr14 not in (None, 0) and tr_today >= 1.3 * atr14)

    # R5 — 21-session up-close vs down-close average volume ratio (close vs prior close)
    up_v, dn_v = [], []
    for i in range(1, len(daily)):
        c, pc, vol = daily[i]["close"], daily[i - 1]["close"], daily[i]["volume"]
        if None in (c, pc, vol):
            continue
        (up_v if c >= pc else dn_v).append(vol)
    up_v, dn_v = up_v[-21:], dn_v[-21:]
    au = sum(up_v) / len(up_v) if up_v else None
    ad = sum(dn_v) / len(dn_v) if dn_v else None
    d["vol21_up_dn"] = (au / ad) if (au is not None and ad not in (None, 0)) else None  # up/down
    d["vol21_dn_up"] = (ad / au) if (ad is not None and au not in (None, 0)) else None  # down/up (SELL)
    d["vol21_au"], d["vol21_ad"] = au, ad   # cc#408: raw up/down-close avg volumes for the detail panel

    # R9 — day VWAP from session bars + close position in the day range
    num = den = 0.0
    for b in bars:
        h, l, c, vol = b["high"], b["low"], b["close"], b.get("volume")
        if None in (h, l, c) or vol is None:
            continue
        num += ((h + l + c) / 3.0) * vol
        den += vol
    d["vwap"] = (num / den) if den else None
    if cmp_v is not None and day_hi is not None and day_lo is not None and day_hi > day_lo:
        d["range_pos"] = (cmp_v - day_lo) / (day_hi - day_lo)   # 0..1 within day range
    else:
        d["range_pos"] = None
    d["above_vwap"] = (cmp_v is not None and d["vwap"] is not None and cmp_v > d["vwap"])

    # R10 — recovery off 2-day low / fall from 2-day high (%). Uses last 2 daily lows/highs + today.
    lows2 = [r["low"] for r in daily[-2:] if r["low"] is not None]
    highs2 = [r["high"] for r in daily[-2:] if r["high"] is not None]
    lo2 = min([x for x in ([day_lo] if day_lo is not None else []) + lows2]) if (lows2 or day_lo is not None) else None
    hi2 = max([x for x in ([day_hi] if day_hi is not None else []) + highs2]) if (highs2 or day_hi is not None) else None
    d["recovery_2d"] = ((cmp_v - lo2) / lo2 * 100.0) if (cmp_v is not None and lo2) else None
    d["fall_from_high_2d"] = ((hi2 - cmp_v) / hi2 * 100.0) if (cmp_v is not None and hi2) else None

    # cc#513 R10 REV-BUY: 5-session low (last 5 daily lows + today's live low from bars).
    lows5 = [r["low"] for r in daily[-5:] if r["low"] is not None]
    if day_lo is not None:
        lows5 = lows5 + [day_lo]
    d["low_5d"] = min(lows5) if lows5 else None

    # R15 — relative strength vs Nifty (stock return − nifty return), weekly & monthly
    wk, mo = v8.get("week_return"), v8.get("month_return")
    nwk, nmo = d.get("nifty_wk"), d.get("nifty_mo")
    d["rs_wk"] = (wk - nwk) if (wk is not None and nwk is not None) else None
    d["rs_mo"] = (mo - nmo) if (mo is not None and nmo is not None) else None

    # R11 — pivot location + room to the next level (%)
    piv = d.get("pivots") or {}
    pp, r1, s1, s2 = piv.get("pp"), piv.get("r1"), piv.get("s1"), piv.get("s2")
    d["above_pp"] = (cmp_v is not None and pp is not None and cmp_v > pp)
    d["room_r1"] = ((r1 - cmp_v) / cmp_v * 100.0) if (cmp_v and r1 is not None) else None
    d["room_s1"] = ((cmp_v - s1) / cmp_v * 100.0) if (cmp_v and s1 is not None) else None
    # cc#513 SELL-MOM R11: distance from CMP down to S2, as %-clearance (V4's own s2c gate).
    d["room_s2"] = ((cmp_v - s2) / cmp_v * 100.0) if (cmp_v and s2 is not None) else None

    # R1 — market-mood tally (breadth + nifty D/W/M). BUY counts fails, SELL counts bullish extremes.
    adr = d.get("adr")
    nday, nwk2, nmo2 = d.get("nifty_day"), d.get("nifty_wk"), d.get("nifty_mo")
    d["mood_fails"] = sum(1 for x in [(adr is not None and adr < 1.0),
                                      (nday is not None and nday < 0),
                                      (nwk2 is not None and nwk2 < 0),
                                      (nmo2 is not None and nmo2 < 0)] if x)
    d["mood_bull"] = sum(1 for x in [(adr is not None and adr > 1.0),
                                     (nday is not None and nday > 0),
                                     (nwk2 is not None and nwk2 > 0),
                                     (nmo2 is not None and nmo2 > 0)] if x)

    # R13 — basis trend (newest first in d["basis"])
    b = d.get("basis") or []
    d["basis_now"] = b[0]["basis_pct"] if b else None
    d["basis_prev"] = b[-1]["basis_pct"] if len(b) >= 2 else None
    d["oi_chg"] = b[0]["oi_chg"] if b else None

    # R16 (cc#410, id=3019): 3M swing position for the fib rule. Swing = last ~63 trading days EOD
    # high/low; position = CMP location in the range (0=low, 100=high, >100 = breakout). 3M range
    # <5% -> fib meaningless -> the rule auto-credits 0.5 (data-quality convention).
    d3 = daily[-63:] if len(daily) >= 63 else daily
    fh = [r["high"] for r in d3 if r["high"] is not None]
    flo = [r["low"] for r in d3 if r["low"] is not None]
    if fh and flo and cmp_v is not None:
        sh, sl = max(fh), min(flo)
        rng = sh - sl
        d["fib_swing_hi"], d["fib_swing_lo"] = sh, sl
        d["fib_pos"] = ((cmp_v - sl) / rng * 100.0) if rng > 0 else None
        d["fib_range_ok"] = (sl > 0 and rng > 0 and (rng / sl * 100.0) >= 5.0)
    else:
        d["fib_swing_hi"] = d["fib_swing_lo"] = d["fib_pos"] = None
        d["fib_range_ok"] = False

    # cc#584: stock 63d return (R19 relative strength). cc#585: ΔGVM-180d (R20 GVM trend).
    d["ret63"] = _ret63(d.get("daily"))
    _g0, _g180 = d.get("gvm_score"), d.get("gvm180")
    d["dgvm180"] = (_g0 - _g180) if (_g0 is not None and _g180 is not None) else None

    # DTE to monthly expiry (G3)
    today = _ist().date()
    exp = _current_expiry(today)
    d["dte"] = (exp - today).days
    return d


# ── shared loaders for R18/R19/R20 (cc#584/585/586) — called by BOTH the single loader and the
#    batch scanner so scanner score == single-symbol score, exactly (SHARED-MODULE CONTRACT) ──

def _ret63(daily):
    """Stock 63-trading-day EOD-close return. None if fewer than 64 clean closes (recent listing)."""
    closes = [b["close"] for b in (daily or []) if b.get("close") is not None]
    if len(closes) < 64:
        return None
    c0, c63 = closes[-1], closes[-64]
    return (c0 / c63 - 1.0) if c63 else None


def _nifty_ret63(cur):
    """NIFTY50 63-trading-day return from raw_prices (symbol=NIFTY50). Shared market-wide read."""
    cur.execute("""SELECT close FROM raw_prices WHERE symbol='NIFTY50' AND close IS NOT NULL
                   ORDER BY price_date DESC LIMIT 64""")
    cl = [_f(r[0]) for r in cur.fetchall()]
    if len(cl) < 64:
        return None
    return (cl[0] / cl[63] - 1.0) if cl[63] else None


def _sector_aggs(cur, segment):
    """mcap-weighted segment averages: m_score (R18 sector-M) + 63d return (R19 sector-RS). Same
    mcap-weighting method as the sector GVM ratings, over all segment constituents (NOT self-excluded
    — this is the sector benchmark, not a peer set). Returns {sector_m, sector_ret63, n_ret}."""
    if not segment:
        return {"sector_m": None, "sector_ret63": None, "n_ret": 0}
    cur.execute("""
        WITH seg AS (
            SELECT g.symbol, g.m_score, g.market_cap
            FROM gvm_scores g
            WHERE g.segment=%s AND g.market_cap IS NOT NULL AND g.market_cap > 0
              AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        ),
        pr AS (
            SELECT s.symbol, s.m_score, s.market_cap,
                   (SELECT close FROM raw_prices WHERE symbol=s.symbol AND close IS NOT NULL
                    ORDER BY price_date DESC LIMIT 1) AS c0,
                   (SELECT close FROM raw_prices WHERE symbol=s.symbol AND close IS NOT NULL
                    AND price_date <= CURRENT_DATE - 63 ORDER BY price_date DESC LIMIT 1) AS c63
            FROM seg s
        )
        SELECT
            SUM(m_score*market_cap) FILTER (WHERE m_score IS NOT NULL)
              / NULLIF(SUM(market_cap) FILTER (WHERE m_score IS NOT NULL), 0)                        AS w_m,
            SUM((c0/c63-1.0)*market_cap) FILTER (WHERE c0 IS NOT NULL AND c63 IS NOT NULL AND c63>0)
              / NULLIF(SUM(market_cap) FILTER (WHERE c0 IS NOT NULL AND c63 IS NOT NULL AND c63>0), 0) AS w_ret,
            COUNT(*) FILTER (WHERE c0 IS NOT NULL AND c63 IS NOT NULL AND c63>0)                     AS n_ret
        FROM pr
    """, (segment,))
    r = cur.fetchone() or (None, None, 0)
    return {"sector_m": _f(r[0]), "sector_ret63": _f(r[1]), "n_ret": int(r[2] or 0)}


def _dgvm180(cur, symbol):
    """gvm_score ~180 calendar days ago (nearest score_date on/before today-180d) from gvm_history.
    None if the symbol has no history that old (recent listing) -> R20 scores 0."""
    cur.execute("""SELECT gvm_score FROM gvm_history WHERE symbol=%s AND gvm_score IS NOT NULL
                   AND score_date <= CURRENT_DATE - 180 ORDER BY score_date DESC LIMIT 1""", (symbol,))
    r = cur.fetchone()
    return _f(r[0]) if r else None


# ── data loader (single symbol) ──────────────────────────────────────────────────

# ── cc#717 part_3: peer set = same-segment gvm_scores (NOT joined to v8_metrics, which holds only the
#    ~210 futures universe and silently zeroed cash peers). Top-10 by market_cap, self excluded; live
#    day% from cmp_prices (else last intraday close) vs the prior raw_prices close — one BULK query, so
#    the single loader and the batch scanner get byte-identical peer numbers (SHARED-MODULE CONTRACT).
#    (Deviation from the spec's literal "resolve_cmp per peer": a bulk day% preserves exact scanner↔
#    single parity AND avoids a Yahoo storm across a whole segment — both hard requirements. The bug it
#    fixes — cash peers appearing at all — is fixed identically either way.)
def _segment_peer_rows(cur, segment):
    if not segment:
        return []
    cur.execute("""
        WITH seg AS (
            SELECT symbol, market_cap FROM gvm_scores
            WHERE segment=%s AND score_date=(SELECT MAX(score_date) FROM gvm_scores)
        )
        SELECT s.symbol, s.market_cap,
               CASE WHEN px.cmp IS NOT NULL AND px.pclose IS NOT NULL AND px.pclose<>0
                    THEN (px.cmp - px.pclose)/px.pclose*100.0 END AS day_pct
        FROM seg s
        LEFT JOIN LATERAL (
            SELECT COALESCE(cp.cmp, li.close) AS cmp, pc.close AS pclose
            FROM (SELECT 1) _
            LEFT JOIN cmp_prices cp ON cp.symbol=s.symbol
            LEFT JOIN LATERAL (SELECT close FROM intraday_prices ip
                               WHERE ip.symbol=s.symbol ORDER BY ip.ts DESC LIMIT 1) li ON TRUE
            LEFT JOIN LATERAL (SELECT close FROM raw_prices rp
                               WHERE rp.symbol=s.symbol AND rp.price_date < CURRENT_DATE
                               ORDER BY rp.price_date DESC LIMIT 1) pc ON TRUE
        ) px ON TRUE
    """, (segment,))
    return [(r[0], (float(r[1]) if r[1] is not None else None), _f(r[2])) for r in cur.fetchall()]


def _peer_counts(rows, self_symbol):
    """Top-10 same-segment peers by market_cap, EXCLUDING self; tally up/down among those with a
    resolvable live day%. peer_count reflects the REAL peers found (cc#717 part_3)."""
    peers = sorted([r for r in (rows or []) if r[0] != self_symbol],
                   key=lambda r: (r[1] if r[1] is not None else -1.0), reverse=True)[:10]
    up1 = up = dn1 = dn05 = dn = n = 0
    for _s, _m, dp in peers:
        if dp is None:
            continue
        n += 1
        if dp > 1:    up1 += 1
        if dp > 0:    up += 1
        if dp < -1:   dn1 += 1
        if dp < -0.5: dn05 += 1
        if dp < 0:    dn += 1
    return {"peers_up1": up1, "peers_up": up, "peers_dn1": dn1,
            "peers_dn05": dn05, "peers_dn": dn, "peer_count": n}


def _load_one(cur, symbol):
    d = {"symbol": symbol}

    cur.execute("""SELECT price_date, open, high, low, close, volume
                   FROM raw_prices WHERE symbol=%s
                   ORDER BY price_date DESC LIMIT 160""", (symbol,))
    rows = [{"price_date": r[0], "open": _f(r[1]), "high": _f(r[2]),
             "low": _f(r[3]), "close": _f(r[4]), "volume": _f(r[5])}
            for r in cur.fetchall()]
    rows.reverse()
    d["daily"] = rows

    d["nifty_day"], d["nifty_wk"], d["nifty_mo"], d["nifty_source"] = live_nifty_dwm(cur)

    vr = volume_ratio(cur, symbol)          # R6 — time-adjusted intraday volume
    d["vol_ratio_today"] = vr["ratio"]

    cur.execute("""SELECT dma_20, dma_50, dma_200, daily_rsi, rsi_month, rsi_weekly,
                          week_return, month_return, mom_2d, week_index_52,
                          sector_week, sector_month, day_1d
                   FROM v8_metrics WHERE symbol=%s
                   ORDER BY score_date DESC LIMIT 1""", (symbol,))
    m = cur.fetchone()
    keys = ["dma_20", "dma_50", "dma_200", "daily_rsi", "rsi_month", "rsi_weekly",
            "week_return", "month_return", "mom_2d", "week_index_52",
            "sector_week", "sector_month", "day_1d"]
    d["v8"] = {k: _f(m[i]) for i, k in enumerate(keys)} if m else {k: None for k in keys}

    cur.execute("""SELECT gvm_score, segment, v_score, m_score FROM gvm_scores WHERE symbol=%s
                   ORDER BY score_date DESC LIMIT 1""", (symbol,))
    g = cur.fetchone()
    d["gvm_score"] = _f(g[0]) if g else None
    d["segment"] = g[1] if g else None
    d["v_score"] = _f(g[2]) if g else None  # cc#583: V-pillar for R17 Valuation rule
    d["m_score"] = _f(g[3]) if g else None  # cc#586: stock M for R18 Momentum

    # cc#584/585/586: R18 sector-M + R19 sector-RS + nifty-RS + R20 ΔGVM180 inputs (shared helpers)
    _sa = _sector_aggs(cur, d["segment"])
    d["sector_m"] = _sa["sector_m"]
    d["sector_ret63"] = _sa["sector_ret63"]
    d["sector_n_ret"] = _sa["n_ret"]
    d["nifty_ret63"] = _nifty_ret63(cur)
    d["gvm180"] = _dgvm180(cur, symbol)

    d.update({"peers_up1": 0, "peers_up": 0, "peers_dn1": 0, "peers_dn05": 0, "peers_dn": 0, "peer_count": 0})
    if d["segment"]:
        # cc#717 part_3: gvm_scores top-10-mcap peers + live day% (shared helpers) — drops the
        # v8_metrics INNER JOIN that zeroed cash peers. Identical numbers in the batch scanner.
        d.update(_peer_counts(_segment_peer_rows(cur, d["segment"]), symbol))

    cur.execute("""SELECT pp, r1, s1, r2, s2 FROM v8_paper_pivots WHERE symbol=%s
                   ORDER BY pivot_date DESC LIMIT 1""", (symbol,))
    p = cur.fetchone()
    d["pivots"] = ({"pp": _f(p[0]), "r1": _f(p[1]), "s1": _f(p[2]), "r2": _f(p[3]), "s2": _f(p[4])}
                   if p else {"pp": None, "r1": None, "s1": None, "r2": None, "s2": None})

    # latest SESSION 5-min bars (with volume) — robust to weekends/frozen (not tied to CURRENT_DATE)
    cur.execute("""SELECT open, high, low, close, volume FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m'
                     AND ts::date = (SELECT MAX(ts::date) FROM intraday_prices
                                     WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m')
                   ORDER BY ts""", (symbol, symbol))
    d["bars"] = [{"open": _f(r[0]), "high": _f(r[1]), "low": _f(r[2]),
                  "close": _f(r[3]), "volume": _f(r[4])} for r in cur.fetchall()]

    cmp_v = d["bars"][-1]["close"] if d["bars"] else None
    if cmp_v is None:
        cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s ORDER BY updated_at DESC LIMIT 1", (symbol,))
        c = cur.fetchone()
        cmp_v = _f(c[0]) if c else None
    if cmp_v is None and rows:
        cmp_v = rows[-1]["close"]
    d["cmp"] = cmp_v

    # cc#717 part_2 / R1 (spec id=9946): ADR = the LIVE V8 mood-gate source = adr_intraday last tick
    # (frozen at the most recent session, cc#719), NOT adr_daily (stale EOD, no row 15:30-15:50). Single
    # source of truth with the V8 gate so TC R1 and the live gate can never disagree again.
    cur.execute("SELECT adr FROM adr_intraday WHERE universe_count >= 50 ORDER BY ts DESC LIMIT 1")
    a = cur.fetchone()
    d["adr"] = _f(a[0]) if a else None

    cur.execute("""SELECT basis_pct, oi_chg FROM futures_basis WHERE symbol=%s
                   ORDER BY ts DESC LIMIT 3""", (symbol,))
    d["basis"] = [{"basis_pct": _f(r[0]), "oi_chg": _f(r[1])} for r in cur.fetchall()]

    # cc#935 / 18064 — R24 Delivery confirm inputs. 3-day average vs the symbol's own trailing
    # baseline of UP TO 21 sessions (see the R24 note for why "up to"). The bulk scanner fills the
    # SAME four fields from the SAME window so both paths score R24 identically.
    cur.execute("""SELECT avg(deliv_pct) FILTER (WHERE rn <= 3),  count(*) FILTER (WHERE rn <= 3),
                          avg(deliv_pct) FILTER (WHERE rn <= 21), count(*) FILTER (WHERE rn <= 21)
                   FROM (SELECT deliv_pct, row_number() OVER (ORDER BY d DESC) rn
                         FROM delivery_eod
                         WHERE UPPER(symbol)=UPPER(%s) AND deliv_pct IS NOT NULL) t""", (symbol,))
    _dl = cur.fetchone()
    d["deliv_3d"] = _f(_dl[0]) if _dl else None
    d["deliv_n3"] = int(_dl[1] or 0) if _dl else 0
    d["deliv_21d"] = _f(_dl[2]) if _dl else None
    d["deliv_n21"] = int(_dl[3] or 0) if _dl else 0

    cur.execute("SELECT 1 FROM futures_universe WHERE UPPER(symbol)=UPPER(%s) AND is_active=TRUE", (symbol,))
    d["is_future"] = cur.fetchone() is not None

    # cc#451: G2 events gate — 3-day earnings lookahead. Capture the imminent result DATE (not just a
    # boolean) so the gate display can cite it ("Results 13-Jul (tomorrow)") from ONE evaluation, and
    # so chip + banner can never disagree. result dates are scraped + refreshed daily (cc#420).
    cur.execute("""SELECT ex_date FROM earnings_calendar
                   WHERE UPPER(ticker)=UPPER(%s) AND ex_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 2
                   ORDER BY ex_date ASC LIMIT 1""",
                (symbol,))
    _ev = cur.fetchone()
    d["event_blackout"] = _ev is not None
    d["event_date"] = _ev[0].isoformat() if _ev and _ev[0] else None

    # cc#677 (spec id=9035, ZERO-VETO): alert-chip inputs. fo_ban = current F&O ban (MWPL); recency-
    # guarded (last 5d) so a stale MAX(d) can't false-report a lifted ban. Result-date chip source =
    # nearest UPCOMING (future) + most-recent REPORTED. All alert-only — never gate the verdict.
    cur.execute("SELECT 1 FROM fo_ban WHERE UPPER(symbol)=UPPER(%s) AND d >= CURRENT_DATE - 5 LIMIT 1", (symbol,))
    d["fo_ban"] = cur.fetchone() is not None
    cur.execute("""SELECT ex_date FROM earnings_calendar WHERE UPPER(ticker)=UPPER(%s)
                   AND status='upcoming' AND ex_date >= CURRENT_DATE ORDER BY ex_date ASC LIMIT 1""", (symbol,))
    _up = cur.fetchone()
    cur.execute("""SELECT ex_date FROM earnings_calendar WHERE UPPER(ticker)=UPPER(%s)
                   AND status='reported' ORDER BY ex_date DESC LIMIT 1""", (symbol,))
    _rep = cur.fetchone()
    d["result_up"] = _up[0].isoformat() if _up and _up[0] else None
    d["result_rep"] = _rep[0].isoformat() if _rep and _rep[0] else None

    return _derive(d)


# ── alert chips (cc#677: ZERO-VETO — informational only, never gate the verdict) ────

def _dm(dt):
    """Uppercase 'DD MON' date label for the alert chips (e.g. '24 JUL', '04 AUG')."""
    try:
        return dt.strftime("%d %b").upper()
    except Exception:
        return "--"


def _result_alert(d):
    """RESULT chip from earnings_calendar. GREEN=recently reported (announced) · RED=due today/T+1 ·
    YELLOW=upcoming beyond T+1 · GREY=no date known. Alert only."""
    today = _ist().date()
    up, rep = d.get("result_up"), d.get("result_rep")
    def _p(iso):
        try:
            return date.fromisoformat(iso) if iso else None
        except Exception:
            return None
    du = _p(up)
    if du:
        delta = (du - today).days
        if delta <= 1:
            return {"type": "result", "color": "red",
                    "label": f"RESULT: {'TODAY' if delta <= 0 else 'TOMORROW'} ({_dm(du)})"}
        return {"type": "result", "color": "yellow", "label": f"RESULT: {_dm(du)}"}
    dr = _p(rep)
    if dr and (today - dr).days <= 12:
        return {"type": "result", "color": "green", "label": f"RESULT: ANNOUNCED {_dm(dr)}"}
    return {"type": "result", "color": "grey", "label": "RESULT: --"}


def _alerts(d):
    """cc#677 (founder-final, spec id=9035): risk conditions render as ALERT CHIPS only — they NEVER
    reject. F&O ban (MWPL, red) · result-date proximity (green/red/yellow/grey) · GVM floor (amber) ·
    near-month DTE countdown (<5, amber). The verdict is SCORE BANDS ALONE — zero veto paths."""
    out = []
    if d.get("fo_ban"):
        out.append({"type": "ban", "color": "red", "label": "F&O BAN · MWPL"})
    out.append(_result_alert(d))
    g = d.get("gvm_score")
    if g is not None and g < 6.5:
        out.append({"type": "gvm", "color": "amber", "label": f"GVM {g:.2f} BELOW FLOOR"})
    dte = d.get("dte")
    if d.get("is_future") and dte is not None and dte < 5:
        out.append({"type": "dte", "color": "amber", "label": f"{dte} {'DAY' if dte == 1 else 'DAYS'} TO EXPIRY"})
    return out


# ── 15 scored rules (style + side aware) ─────────────────────────────────────────

def _rules(d, style, side):
    """cc#400 (session_log id=3010): SELL side recalibrated. BUY side UNTOUCHED.
    SELL drops R6 (vol-today) and R14 (ATR ignition) -> max 13. Killer rules relaxed to the
    live-validated sell conditions (V5-D / SellMom-N5 / SellOB-V3). R3/R9 are session-anchored
    by the loaders (last trading session, not CURRENT_DATE)."""
    MOM = style == "MOMENTUM"
    BUY = side == "BUY"
    v8 = d.get("v8") or {}
    out = []

    # R1 — market mood
    if BUY:
        f = d.get("mood_fails", 0)
        out.append(_R("R1", "Mood (fails)", 1.0 if f <= 1 else (0.5 if f == 2 else 0.0), f,
                      required="mood fails <= 1 (2 = 0.5, >=3 = 0)"))
    else:
        # 3010: binary — any mood check bearish -> 1; all-bullish -> 0
        f = d.get("mood_fails", 0)
        out.append(_R("R1", "Mood (any bearish)", 1.0 if f >= 1 else 0.0, f,
                      required="any mood check bearish (fails >= 1)"))

    # R2 — sector (unchanged; already matches 3010)
    sw, sm = v8.get("sector_week"), v8.get("sector_month")
    if MOM:
        c = _both((sw or 0) > 0, (sm or 0) > 0) if BUY else _both((sw or 0) < 0, (sm or 0) < 0)
        req = f"sector wk & mo {'> 0' if BUY else '< 0'} (one leg = 0.5)"
    else:
        pos = (sm is not None and (sm > 0 if BUY else sm < 0))
        c = 1.0 if pos else 0.0
        req = f"sector mo {'> 0' if BUY else '< 0'}"
    out.append(_R("R2", f"Sector {'wk&mo' if MOM else 'mo'}", c, {"wk": _r(sw), "mo": _r(sm)}, required=req))

    # R3 — peers in same segment (<3 peers -> auto 0.5). 3010 SELL MOM uses down>0.5% for the strong tier.
    if d.get("peer_count", 0) < 3:
        c = 0.5
        req = "peer_count >= 3 (else auto 0.5)"
    else:
        if BUY:
            strong, any_dir = d.get("peers_up1", 0), d.get("peers_up", 0)
        else:
            strong, any_dir = d.get("peers_dn05", 0), d.get("peers_dn", 0)
        if MOM:
            c = 1.0 if strong >= 2 else (0.5 if any_dir >= 1 else 0.0)
            req = f"{'>=2 peers up >1%' if BUY else '>=2 peers dn >0.5%'} (>=1 any-dir = 0.5)"
        else:
            c = 1.0 if any_dir >= 2 else (0.5 if any_dir >= 1 else 0.0)
            req = f"{'>=2 peers up' if BUY else '>=2 peers dn'} (1 = 0.5)"
    out.append(_R("R3", "Peers", c, {"strong": (d.get("peers_up1") if BUY else d.get("peers_dn05")),
                                     "dir": (d.get("peers_up") if BUY else d.get("peers_dn")),
                                     "n": d.get("peer_count")}, required=req))

    # R4 — moving averages. cc#513: BUY-MOM full credit now requires dma_20>0 among the 2-of-3
    # (BuyMom V3's own dma_20>0 hard gate) -- SELL-MOM / REV (both sides) UNCHANGED.
    above = [(v8.get("dma_20") or 0) > 0, (v8.get("dma_50") or 0) > 0, (v8.get("dma_200") or 0) > 0]
    below = [(v8.get("dma_20") or 0) < 0, (v8.get("dma_50") or 0) < 0, (v8.get("dma_200") or 0) < 0]
    if MOM:
        if BUY:
            d20ok = (v8.get("dma_20") or 0) > 0
            n = sum(above)
            c = 1.0 if (n >= 2 and d20ok) else (0.5 if n >= 1 else 0.0)
            req = "2-of-3 DMAs up incl. dma_20>0 (1-of-3 or missing dma_20 = 0.5)"
        else:
            n = sum(below)
            c = 1.0 if n >= 2 else (0.5 if n == 1 else 0.0)
            req = "2-of-3 DMAs down (1-of-3 = 0.5)"
    elif BUY:
        # cc#767 BUY-REV: graded — dma_200>0 AND dma_20>0 = 1, one = 0.5 (V6.1 gate; was binary dma_200).
        d200p = (v8.get("dma_200") or 0) > 0
        d20p = (v8.get("dma_20") or 0) > 0
        c = 1.0 if (d200p and d20p) else (0.5 if (d200p or d20p) else 0.0)
        req = "dma_200>0 AND dma_20>0 (one = 0.5)"
    else:
        # cc#767 SELL-REV: graded — all three DMAs < 0 = 1, two = 0.5 (V6.1 mirror; was binary dma_200<0).
        nb = sum(below)
        c = 1.0 if nb == 3 else (0.5 if nb == 2 else 0.0)
        req = "3 DMAs < 0 (2/3 = 0.5)"
    out.append(_R("R4", f"MAs {'2of3' if MOM else 'graded'}", c,
                  {"d20": _r(v8.get("dma_20")), "d50": _r(v8.get("dma_50")), "d200": _r(v8.get("dma_200"))},
                  required=req))

    # R5 — cc#767 VOLUME CONFIRM: merges old R5 (1M up/dn ratio) + R6 (today time-adjusted) into ONE
    # rule tested once. today >= 1.5x OR 1M >= 1.1 -> 1pt; a partial leg (today >= 1.1x OR 1M >= 0.9) ->
    # 0.5. SELL mirror uses the down/up 1M ratio (>=1.05 full / >=0.95 partial) with the same today surge
    # leg — a volume surge confirms conviction on either side. (Old R14 ATR ignition dropped entirely;
    # R9 5m+VWAP already answers "is it moving now".)
    vt = d.get("vol_ratio_today")
    if BUY and not MOM:
        # cc#934 / 18062 amendment 3: on BUY-REV the two tests score INDEPENDENTLY — a burst today
        # and a month of accumulation are different pieces of evidence, and the old OR-with-halves
        # let either one alone cap the rule. Both = 2, one = 1, none = 0. No half credits here; the
        # old partial thresholds (1.1x / 0.9) are dropped on this card only.
        v1m = d.get("vol21_up_dn")
        leg_a = (vt is not None and vt >= 1.5)
        leg_b = (v1m is not None and v1m >= 1.1)
        c = float(leg_a) + float(leg_b)
        req = "today>=1.5x pace = 1 + 1M up/dn>=1.1 = 1 (independent, max 2)"
        out.append(_R("R5", "Volume confirm", c,
                      {"today": _r(vt), "vol1M": _r(v1m), "leg_today": leg_a, "leg_1m": leg_b},
                      required=req, max_credit=2.0))
    else:
        if BUY:
            v1m = d.get("vol21_up_dn")
            full = ((vt is not None and vt >= 1.5) or (v1m is not None and v1m >= 1.1))
            part = ((vt is not None and vt >= 1.1) or (v1m is not None and v1m >= 0.9))
            req = "today>=1.5x OR 1M up/dn>=1.1 (partial today>=1.1x/1M>=0.9 = 0.5)"
        else:
            v1m = d.get("vol21_dn_up")
            full = ((vt is not None and vt >= 1.5) or (v1m is not None and v1m >= 1.05))
            part = ((vt is not None and vt >= 1.1) or (v1m is not None and v1m >= 0.95))
            req = "today>=1.5x OR 1M dn/up>=1.05 (partial = 0.5)"
        c = 1.0 if full else (0.5 if part else 0.0)
        out.append(_R("R5", "Volume confirm", c, {"today": _r(vt), "vol1M": _r(v1m)}, required=req))

    # R7 — RSI. cc#513 cross-cutting fix: MOM (both sides) now reads true_weekly_rsi, not the
    # synthetic rsi_weekly (~16pt off, cc#353) -- synthetic must not appear in any rule after this.
    # REV drops the dRSI leg entirely -> a pure weekly-heat/weekly-cool band on true_weekly_rsi.
    mr = v8.get("rsi_month")
    twr = d.get("true_weekly_rsi")
    if MOM:
        if BUY:
            mr_ok = (mr is not None and mr >= 50)
            twr_band = (1.0 if (twr is not None and 70 <= twr <= 85)
                        else (0.5 if (twr is not None and ((60 <= twr < 70) or (85 < twr <= 90))) else 0.0))
            c = twr_band if mr_ok else min(twr_band, 0.5)
            req = "mRSI>=50 AND twr in [70,85] (twr [60,70)/(85,90] or mRSI<50 = 0.5 cap)"
        else:
            c = _both(mr is not None and mr < 40, twr is not None and twr <= 40)
            req = "mRSI<40 AND twr<=40 (one leg = 0.5)"
        val = {"mRSI": _r(mr), "trueWk": _r(twr)}
        label = "RSI frame"
    elif BUY:
        # cc#767 BUY-REV: monthly RSI in [60,90] = 1 (V6.1 gate). Replaces the dead V5 remnant twr>=70
        # (V6.1 removed it — a true dip candidate is quality-uptrend, weekly-cool, not weekly-hot).
        c = (1.0 if (mr is not None and 60 <= mr <= 90)
             else (0.5 if (mr is not None and ((50 <= mr < 60) or (90 < mr <= 95))) else 0.0))
        req = "mRSI in [60,90] (50-60 or 90-95 = 0.5)"
        val = {"mRSI": _r(mr)}
        label = "RSI monthly"
    else:
        # SELL-REV: weekly-cool band on true weekly RSI (unchanged).
        c = 1.0 if (twr is not None and twr <= 45) else (0.5 if (twr is not None and 45 < twr <= 50) else 0.0)
        req = "twr <= 45 (45-50 = 0.5)"
        val = {"trueWk": _r(twr)}
        label = "RSI weekly heat"
    out.append(_R("R7", label, c, val, required=req))

    # R8 — returns. cc#513 BUY-REV: V5's own month_return cap + sanity floor replaces mo>0.
    wk, mo = v8.get("week_return"), v8.get("month_return")
    if MOM:
        c = _both((wk or 0) > 0, (mo or 0) > 0) if BUY else _both((wk or 0) < 0, (mo or 0) < 0)
        req = f"wk & mo {'> 0' if BUY else '< 0'} (one leg = 0.5)"
    elif BUY:
        c = 1.0 if (mo is not None and -10 <= mo < 5) else 0.0
        req = "mret in [-10, 5)"
    else:
        c = 1.0 if (mo is not None and mo < 0) else 0.0
        req = "mo < 0"
    # cc#935 / 18064: R8 is DROPPED from BUY-MOM — the returns dimension is already carried four times
    # over on that card (R7 RSI, R10 mom_2d, R18 M-score which is derived from returns, R22 52w index),
    # so it adds no independent information. It stays live on BUY-REV and both SELL cards.
    if not (BUY and MOM):
        out.append(_R("R8", f"Returns {'wk&mo' if MOM else 'mo'}", c, {"wk": _r(wk), "mo": _r(mo)}, required=req))

    # R9 — 5-min structure + VWAP (session-anchored via last-session bars). 3010 SELL: below-VWAP is the
    #      gate for any credit — full (below-VWAP + weak structure) ->1, below-VWAP only ->0.5, else 0.
    upper = (d.get("range_pos") is not None and d["range_pos"] >= 0.5)
    lower = (d.get("range_pos") is not None and d["range_pos"] < 0.5)
    av = d.get("above_vwap")
    below_vwap = (av is False and d.get("vwap") is not None)
    if BUY:
        if MOM:
            c = _both(av, upper)
            req = "above VWAP AND upper day-range (one leg = 0.5)"
        else:
            c = _both((d.get("recovery_2d") or 0) > 0, av)
            req = "recovering off low AND above VWAP (one leg = 0.5)"
    else:
        second = lower if MOM else ((d.get("fall_from_high_2d") or 0) > 0)
        c = 1.0 if (below_vwap and second) else (0.5 if below_vwap else 0.0)
        req = f"below VWAP AND {'lower day-range' if MOM else 'falling from high'} (below-VWAP only = 0.5)"
    out.append(_R("R9", "5m + VWAP", c, {"vwap": _r(d.get("vwap")), "rangePos": _r(d.get("range_pos"))}, required=req))

    # R10 — style extras. cc#513: BUY-MOM w52 gets the V3 hard-gate tier (>=75 full); BUY-REV swaps
    # recovery-2-8% for S1-context (5d low vs S1); SELL-MOM/-REV bands refreshed to V4/V6.1.
    m2 = v8.get("mom_2d")
    w52 = v8.get("week_index_52")
    if MOM:
        if BUY:
            # cc#767 BUY-MOM: mom_2d in [0,6] ONLY — w52>=75 is now its own imported V3 hard-gate rule
            # (appended below), tested once, not folded here.
            c = 1.0 if (m2 is not None and 0 <= m2 <= 6) else (0.5 if (m2 is not None and -1 <= m2 < 0) else 0.0)
            req = "mom_2d in [0,6] ([-1,0) = 0.5)"
        else:
            c = _both(m2 is not None and -4 <= m2 <= -2, w52 is not None and 20 <= w52 <= 60)
            req = "mom_2d in [-4,-2] AND w52 in [20,60] (one leg = 0.5)"
        val = {"mom_2d": _r(m2), "w52": _r(w52)}
    else:
        if BUY:
            low5d, s1 = d.get("low_5d"), (d.get("pivots") or {}).get("s1")
            if low5d is not None and s1 is not None:
                if low5d <= s1:
                    c = 1.0
                elif s1 != 0 and abs(low5d - s1) / abs(s1) <= 0.01:
                    c = 0.5
                else:
                    c = 0.0
            else:
                c = 0.0
            val = {"low_5d": _r(low5d), "s1": _r(s1)}
            req = "5d low <= S1 (within 1% of S1 = 0.5)"
        else:
            # cc#767 SELL-REV: turn = mom_2d in [-4,-1] AND week_index_52 < 50 (V6.1 gate). Replaces the
            # old fall-from-high band, which overlapped R9's falling-from-high leg (tested twice).
            m2ok = (m2 is not None and -4 <= m2 <= -1)
            w52ok = (w52 is not None and w52 < 50)
            c = 1.0 if (m2ok and w52ok) else (0.5 if (m2ok or w52ok) else 0.0)
            val = {"mom_2d": _r(m2), "w52": _r(w52)}
            req = "mom_2d in [-4,-1] AND w52 < 50 (one leg = 0.5)"
    out.append(_R("R10", "Style extra", c, val, required=req))

    # R11 — location + room. cc#513 BUY-REV: room-to-R1 removed (V5 = fixed +/-3 exits) -> V5 turn
    # conditions (mom_2d/week_return). SELL-MOM: S1-room replaced by S2-clearance (V4's own s2c gate).
    if MOM and BUY:
        room_next = d.get("room_r1")
        if not d.get("above_pp"):
            c = 0.0
        elif room_next is not None and room_next >= 2:
            c = 1.0
        else:
            c = 0.5
        val = {"abovePP": d.get("above_pp"), "room": _r(room_next)}
        req = "above PP AND room-to-R1 >= 2% (above PP only = 0.5)"
    elif MOM and (not BUY):
        # cc#767 SELL-MOM: R11 is now CMP < PP alone (a distinct 1pt gate); S2-clearance is split into
        # its own imported V8-survival rule (appended below), tested once each rather than bundled.
        c = 0.0 if d.get("above_pp") else 1.0
        val = {"abovePP": d.get("above_pp")}
        req = "CMP < PP"
    elif (not MOM) and BUY:
        m2r, wkr = v8.get("mom_2d"), v8.get("week_return")
        n = sum(1 for x in [(m2r is not None and m2r >= -0.5), (wkr is not None and wkr >= -2)] if x)
        c = 1.0 if n == 2 else (0.5 if n == 1 else 0.0)
        val = {"mom_2d": _r(m2r), "week_return": _r(wkr)}
        req = "mom_2d >= -0.5 AND week_return >= -2 (one leg = 0.5)"
    else:   # SELL-REV — unchanged (S1-room band)
        room_next = d.get("room_s1")
        if room_next is None:
            c = 0.0
        elif room_next >= 2:
            c = 1.0
        elif room_next >= 1:
            c = 0.5
        else:
            c = 0.0
        val = {"abovePP": d.get("above_pp"), "room": _r(room_next)}
        req = "S1-room >= 2% (1-2% = 0.5)"
    out.append(_R("R11", "Location + room", c, val, required=req))

    # R12 — cc#767 DERIVATIVES CONFIRM: merges old R12 (OI structure) + R13 (basis) into ONE rule tested
    # once, replacing the two-way split. Two legs — (1) OI change present with the aligned price
    # direction, (2) basis moving the aligned direction. Both legs win = 1pt, one leg = 0.5, neither
    # (data present) = 0, both legs missing/stale = 0.5 neutral. Cash (non-futures) stocks have no
    # derivatives -> 0.5 N/A + CASH chip. (Old R14 ATR ignition dropped entirely — see the R5 note; R9
    # 5m+VWAP already answers "is it moving now".)
    day = v8.get("day_1d")
    oic = d.get("oi_chg")
    now, prev = d.get("basis_now"), d.get("basis_prev")
    if not d.get("is_future"):
        out.append(_R("R12", "Derivatives confirm", 0.5,
                      {"cash_na": True, "chip": "CASH · DERIVATIVES N/A"},
                      required="cash stock — derivatives N/A (0.5)"))
    else:
        oi_avail = oic is not None
        basis_avail = (now is not None and prev is not None)
        oi_win = oi_avail and (day is not None and (day > 0 if BUY else day < 0))
        if not basis_avail:
            basis_win = False
        elif BUY:
            basis_win = (now > prev and now > 0) if MOM else (now > prev and now < 0)
        else:
            basis_win = (now < prev and now < 0) if MOM else (now < prev and now > 0)
        if not oi_avail and not basis_avail:
            c = 0.5   # both legs stale/missing -> neutral (was the symmetric R12/R13 missing = 0.5)
        else:
            wins = (1 if oi_win else 0) + (1 if basis_win else 0)
            c = 1.0 if wins >= 2 else (0.5 if wins == 1 else 0.0)
        out.append(_R("R12", "Derivatives confirm", c,
                      {"day": _r(day), "oi_chg": _r(oic), "basis_now": _r(now), "basis_prev": _r(prev)},
                      required="OI-change present (aligned) + basis direction (one leg = 0.5)"))

    # R15 — relative strength vs Nifty. cc#513 BUY-REV: rs_mo band relaxed one notch (dip candidates
    # lag by construction).
    rw, rm = d.get("rs_wk"), d.get("rs_mo")
    if MOM:
        c = _both((rw or 0) > 0, (rm or 0) > 0) if BUY else _both((rw or 0) < 0, (rm or 0) < 0)
        val = {"rs_wk": _r(rw), "rs_mo": _r(rm)}
        req = f"rs_wk & rs_mo {'> 0' if BUY else '< 0'} (one leg = 0.5)"
    else:
        if rm is None:
            c = 0.0
            req = "rs_mo required"
        elif BUY:
            c = 1.0 if rm > -1 else (0.5 if -3 <= rm <= -1 else 0.0)
            req = "rs_mo > -1 (-3 to -1 = 0.5)"
        else:
            c = 1.0 if rm < 0 else (0.5 if 0 <= rm <= 1 else 0.0)
            req = "rs_mo < 0 (0 to 1 = 0.5)"
        val = {"rs_mo": _r(rm)}
    # cc#934 / 18062: R15 is DROPPED from BUY-REV — it duplicates R19, which already scores 63-day
    # relative strength against BOTH the sector and NIFTY. cc#935 / 18064 extends the same drop to
    # BUY-MOM for consistency (same duplication, same R19). It stays live on both SELL cards.
    if not BUY:
        out.append(_R("R15", "RS vs Nifty", c, val, required=req))

    # R16 — Fib position (3M swing). cc#410/id=3019 zones; cc#513 BUY-REV widened (spring fires from
    # Decision AND Strength zones).
    pos = d.get("fib_pos")
    if not d.get("fib_range_ok") or pos is None:
        c = 0.5   # 3M range <5% (or no swing) -> meaningless -> auto 0.5
        req = "3M range >= 5% (else auto 0.5)"
    elif MOM and BUY:      # BUY-MOM: 1 >100 or 61.8-78.6 | 0.5 78.6-100 or 50-61.8 | 0 <50
        c = 1.0 if (pos > 100 or (61.8 <= pos < 78.6)) else (0.5 if ((78.6 <= pos <= 100) or (50 <= pos < 61.8)) else 0.0)
        req = "pos > 100 or 61.8-78.6% (78.6-100% or 50-61.8% = 0.5)"
    elif (not MOM) and BUY:  # cc#513 BUY-REV: 1.0 for 38.2-78.6 (Decision + Strength), 0.5 above, 0 only <23.6
        c = 0.0 if pos < 23.6 else (1.0 if 38.2 <= pos <= 78.6 else 0.5)
        req = "pos 38.2-78.6% (0 only < 23.6%)"
    elif MOM and (not BUY):  # SELL-MOM: 1 <23.6 | 0.5 23.6-38.2 | 0 >38.2
        c = 1.0 if pos < 23.6 else (0.5 if (23.6 <= pos <= 38.2) else 0.0)
        req = "pos < 23.6% (23.6-38.2% = 0.5)"
    else:                    # SELL-REV: 1 78.6-100 | 0 only <61.8 | else 0.5 (>100 breakout=caution)
        c = 0.0 if pos < 61.8 else (1.0 if (78.6 <= pos <= 100) else 0.5)
        req = "pos 78.6-100% (0 only < 61.8%)"
    out.append(_R("R16", "Fib position (3M)", c,
                  {"pos": _r(pos), "zone": _fib_zone_name(pos), "range_ok": d.get("fib_range_ok")}, required=req))

    # R17 — Valuation (V-pillar). cc#583: gvm_scores.v_score bands, identical for BUY + SELL (a cheap
    # name is a better long AND a safer short-cover risk; an expensive name cuts both). ADD (not replace).
    vsc = d.get("v_score")
    _rev_buy = (BUY and not MOM)          # cc#934: BUY-REV only; the other three cards keep 7.5
    if vsc is None:
        vc = 0.0
        vreq = ("V > 7 (6.0-7.0 = 0.5); no V-score -> 0" if _rev_buy
                else "V >= 7.5 (6.0-7.5 = 0.5); no V-score -> 0")
    elif _rev_buy:
        # cc#934 / 18062: full credit strictly ABOVE 7 (founder: "keep R17, bar at 7").
        vc = 1.0 if vsc > 7.0 else (0.5 if vsc >= 6.0 else 0.0)
        vreq = "V > 7 (6.0-7.0 = 0.5, < 6.0 = 0)"
    else:
        vc = 1.0 if vsc >= 7.5 else (0.5 if vsc >= 6.0 else 0.0)
        vreq = "V >= 7.5 (6.0-7.5 = 0.5, < 6.0 = 0)"
    out.append(_R("R17", "Valuation (V)", vc, {"v_score": _r(vsc)}, required=vreq))

    # R18 — Momentum (stock-M + mcap-weighted sector-M). cc#586 (id=6624). BUY rewards strong momentum
    # both; SELL MIRRORED around the scale-midpoint (weak momentum = short candidate).
    sm = d.get("m_score"); secm = d.get("sector_m")
    if BUY and not MOM:
        # cc#934 / 18062: RELAXED for reversal context. The old 7.5/7 bar was structurally
        # contradictory on a dip card — Fable's 08-Aug read of 76 S1-touchers found average M 5.65
        # with only 14 at M>=7.5, so the rule was near-unscoreable exactly where it is meant to help.
        c1 = (sm is not None and sm >= 6.5); c2 = (secm is not None and secm >= 6.0)
        r18req = "stock M>=6.5 AND sector M>=6 (one = 1)"
    elif BUY:
        c1 = (sm is not None and sm >= 7.5); c2 = (secm is not None and secm >= 7.0)
        r18req = "stock M>=7.5 AND sector M>=7 (one = 1)"
    else:
        c1 = (sm is not None and sm <= 2.5); c2 = (secm is not None and secm <= 3.0)
        r18req = "stock M<=2.5 AND sector M<=3 (mirror; one = 1)"
    r18 = 2.0 if (c1 and c2) else (1.0 if (c1 or c2) else 0.0)
    out.append(_R("R18", "Momentum (stock+sector M)", r18,
                  {"m": _r(sm), "sector_m": _r(secm)}, required=r18req, max_credit=2.0))

    # R19 — Relative Strength (63d stock return vs mcap-weighted sector + vs NIFTY50). cc#584 (id=6622).
    # SELL MIRRORED (a laggard scores high). Sector fallback: <3 clean-63d constituents -> nifty-only
    # (effective max 1). No 63d stock history (recent listing) -> 0.
    rst = d.get("ret63"); rnif = d.get("nifty_ret63"); rsec = d.get("sector_ret63")
    sec_ok = (rsec is not None and (d.get("sector_n_ret") or 0) >= 3)
    if rst is None:
        r19 = 0.0; r19req = "no 63d history -> 0"
        r19ev = {"ret63": None}
    else:
        rs_nif = (rst - rnif) if rnif is not None else None
        rs_sec = (rst - rsec) if sec_ok else None
        if BUY:
            r19 = float(sum(1 for x in (rs_sec, rs_nif) if x is not None and x > 0))
        else:  # mirror: lagging both = 2
            r19 = float(sum(1 for x in (rs_sec, rs_nif) if x is not None and x < 0))
        r19req = ("RS vs sector & nifty (both>0=2 / one=1 / SELL mirror: lagging)"
                  if sec_ok else "sector <3 clean peers -> NIFTY-only (max 1)")
        r19ev = {"ret63_pct": _r(rst * 100), "rs_nifty_pct": _r(rs_nif * 100) if rs_nif is not None else None,
                 "rs_sector_pct": _r(rs_sec * 100) if rs_sec is not None else None,
                 "sector_n": d.get("sector_n_ret")}
    out.append(_R("R19", "Relative strength (63d)", r19, r19ev, required=r19req, max_credit=2.0))

    # R20 — GVM trend (ΔGVM 180d, graduated). cc#585 (id=6623). SELL MIRRORED (deteriorating quality =
    # good short). No 180d history -> 0.
    dg = d.get("dgvm180")
    if dg is None:
        r20 = 0.0; r20req = "no 180d history -> 0"
    elif BUY:
        r20 = 1.0 if dg > 0.5 else (0.5 if dg >= 0 else 0.0)
        r20req = "ΔGVM180 >0.5=1 / 0-0.5=0.5 / <0=0"
    else:
        r20 = 1.0 if dg < -0.5 else (0.5 if dg <= 0 else 0.0)
        r20req = "ΔGVM180 <-0.5=1 / -0.5-0=0.5 / >0=0 (mirror)"
    out.append(_R("R20", "GVM trend (Δ180d)", r20, {"dgvm180": _r(dg)}, required=r20req))

    # ── cc#767 imported LIVE-V8 basket hard gates (funded by the flow consolidation: R5+R6 merged,
    # R12+R13 merged, R14 dropped). Each is scored ONCE as its own point — never a veto (cc#677
    # ZERO-VETO holds; these add base score, distinct from R17 V-pillar / R20 GVM-trend). ──────────
    gvm = d.get("gvm_score")
    d50 = v8.get("dma_50")
    w52g = v8.get("week_index_52")
    if BUY and MOM:
        # R21 — dma_50 in [5,12] band (BuyMom V3 hard gate: healthy but not overextended above 50DMA).
        c21 = (1.0 if (d50 is not None and 5 <= d50 <= 12)
               else (0.5 if (d50 is not None and ((2 <= d50 < 5) or (12 < d50 <= 20))) else 0.0))
        out.append(_R("R21", "dma_50 band", c21, {"dma_50": _r(d50)}, required="dma_50 in [5,12] ([2,5)/(12,20] = 0.5)"))
        # R22 — 52-week index >= 75 (BuyMom V3 hard gate: near 52w highs). Extracted from the old R10.
        c22 = (1.0 if (w52g is not None and w52g >= 75) else (0.5 if (w52g is not None and 60 <= w52g < 75) else 0.0))
        out.append(_R("R22", "52w index", c22, {"w52": _r(w52g)}, required="week_index_52 >= 75 (60-75 = 0.5)"))
        # R23 — GVM quality floor >= 7 (BuyMom V3 level, stricter than BuyRev 6.5).
        c23 = (1.0 if (gvm is not None and gvm >= 7.0) else (0.5 if (gvm is not None and 6.5 <= gvm < 7.0) else 0.0))
        out.append(_R("R23", "GVM floor", c23, {"gvm": _r(gvm)}, required="GVM >= 7.0 (6.5-7.0 = 0.5)"))
        # R24 — DELIVERY CONFIRM (cc#935, founder-locked 18064). BUY-MOM only, 1 point. Self-relative
        # by design: the 3-day delivery average is measured against the SYMBOL'S OWN trailing baseline,
        # so what scores is the RISE in cash conviction, not the level (a 90%-delivery utility and a
        # 30%-delivery high-beta name are judged on the same footing). Source: delivery_eod.
        # BASELINE WINDOW — the spec says "own 21d avg". delivery_eod only starts 20-Jul-2026, so the
        # deepest baseline available today is 14 sessions and NO symbol has 21. Taken literally, the
        # rule would score 0 for every symbol on every card — a new point that can never be earned.
        # It is therefore "the trailing UP-TO-21 sessions, minimum 10", which is exactly what Fable's
        # 08-Aug calibration measured: on the 206-symbol active futures universe this reproduces its
        # quoted split EXACTLY (full 10, half 35 — verified against the live table, not assumed). The
        # window includes the 3 recent sessions (excluding them gives 25/36 and does NOT match), and it
        # widens to a true 21 on its own as history accrues — no code change needed.
        # Thin history / no rows -> 0 with an honest note, the same pattern as a missing V-score (R17).
        a3, a21 = d.get("deliv_3d"), d.get("deliv_21d")
        n3, n21 = d.get("deliv_n3") or 0, d.get("deliv_n21") or 0
        if a3 is None or a21 is None or n3 < 3 or n21 < 10 or a21 <= 0:
            c24 = 0.0
            r24val = {"deliv_3d": _r(a3), "deliv_base": _r(a21), "days": n21, "no_data": True}
            r24req = "no delivery history (needs 3 recent + 10 baseline sessions) -> 0"
        else:
            ratio = a3 / a21
            c24 = 1.0 if ratio >= 1.2 else (0.5 if ratio >= 1.1 else 0.0)
            r24val = {"deliv_3d": _r(a3), "deliv_base": _r(a21), "ratio": _r(ratio), "days": n21}
            r24req = "3d avg deliv% >= 1.2x own baseline (1.1-1.2x = 0.5)"
        out.append(_R("R24", "Delivery confirm", c24, r24val, required=r24req))
    elif BUY and (not MOM):
        # R23 — GVM quality floor >= 6.5 (BuyRev V6.1 gate).
        c23 = (1.0 if (gvm is not None and gvm >= 6.5) else (0.5 if (gvm is not None and 6.0 <= gvm < 6.5) else 0.0))
        out.append(_R("R23", "GVM floor", c23, {"gvm": _r(gvm)}, required="GVM >= 6.5 (6.0-6.5 = 0.5)"))
    elif (not BUY) and MOM:
        # R21 — S2-clearance (CMP-S2)/CMP >= 3% (SellMom-N5 V8 survival gate: real room below target).
        rs2 = d.get("room_s2")
        c21 = (1.0 if (rs2 is not None and rs2 >= 3) else (0.5 if (rs2 is not None and 2 <= rs2 < 3) else 0.0))
        out.append(_R("R21", "S2-clearance", c21, {"room_s2": _r(rs2)}, required="S2-clearance >= 3% (2-3% = 0.5)"))

    return out


_STRONG_RATIO = 0.84   # cc#767: preserves the prior BUY strictness (18.5/22 = 0.841)
_VALID_RATIO = 0.65    # cc#767: preserves the prior BUY floor  (14.4/22 = 0.655)


def _verdict(score, max_score):
    """cc#767: DYNAMIC denominator (score_denominator rule, propagation map id=265). After the V8-gate
    imports + flow consolidation each style bucket carries its OWN max (see card_maxes() — derived,
    never listed here), so bands are re-baselined PROPORTIONALLY off the card's own max — preserving the
    founder-locked strictness ratios (STRONG 0.84x / VALID 0.65x, from the prior BUY 18.5-22 / 14.4-22)."""
    if score >= round(_STRONG_RATIO * max_score, 1):
        return "STRONG"
    if score >= round(_VALID_RATIO * max_score, 1):
        return "VALID"
    return "REJECT"


def card_maxes():
    """cc#935 — the four card maxes + bands, DERIVED by running the rulebook over an empty symbol.
    Every rule is emitted regardless of data (a missing input scores 0, it never removes the rule),
    so the sum of the declared maxes is exactly what a real symbol is scored out of. This exists so
    that no surface has to write a max down: the footers and the health block read it from here, and
    the moment a rule is added, dropped or reweighted every number moves with it in one place."""
    out = {}
    for side in ("BUY", "SELL"):
        for style in ("MOMENTUM", "REVERSAL"):
            rules = _rules({}, style, side)
            mx = round(sum(r.get("max", 1.0) for r in rules), 2)
            if float(mx).is_integer():
                mx = int(mx)
            out[f"{side}-{style[:3]}"] = {"max": mx, "rules": len(rules),
                                          "strong": round(_STRONG_RATIO * mx, 1),
                                          "valid": round(_VALID_RATIO * mx, 1)}
    return out


def score_card(d, style, side):
    """The one shared scorer. Returns the full card for one (style, side).
    cc#767 V8-ALIGNMENT: flow rules consolidated (R5+R6 -> Volume confirm, R12+R13 -> Derivatives
    confirm, R14 ATR dropped); freed points reallocated to per-bucket live-V8 basket gate imports
    (BUY-MOM dma_50/w52/GVM, BUY-REV mRSI/GVM/graded-MAs, SELL-MOM CMP<PP + S2-clearance, SELL-REV
    graded 3-DMA + mom_2d/w52).
    cc#934: max is DERIVED — sum(rule.max) over the rules this card actually emitted. It is no
    longer len(rules)+2, which assumed R18/R19 were the only 2-point rules."""
    rules = _rules(d, style, side)
    score = round(sum(r["credit"] for r in rules), 2)
    # cc#934: DERIVED, never assumed. Was `len(rules) + 2`, which hard-coded "R18 and R19 are the
    # only 2-point rules" — untrue the moment Volume became 2 on BUY-REV. Summing each rule's own
    # declared max means the denominator cannot disagree with the rulebook, and no card's max is
    # written down anywhere as a literal.
    max_score = round(sum(r.get("max", 1.0) for r in rules), 2)
    if float(max_score).is_integer():
        max_score = int(max_score)
    # cc#934 item 5: DISPLAY sequence 1..N in evaluation order, with no gaps. The internal R-codes
    # jump (R5->R7, R12->R15) from years of consolidations and drops, and they are referenced by
    # locked specs (3019, 6621-6624, 12289), so they are NEVER renumbered — they travel as
    # legacy_id. Surfaces render `seq`; the payload keeps both.
    for i, r in enumerate(rules, 1):
        r["seq"] = i
        r["legacy_id"] = r["rule"]
    strong = round(_STRONG_RATIO * max_score, 1)
    valid = round(_VALID_RATIO * max_score, 1)
    card = {"style": style, "side": side, "label": f"{side}-{style[:3]}",
            "score": score, "max": max_score, "verdict": _verdict(score, max_score), "rules": rules}
    card["bands"] = f"STRONG≥{strong} / VALID {valid}–{strong} / REJECT<{valid}"
    if side == "SELL":
        card["recal"] = "V8-ALIGNED cc#767"
    return card


# ── public compute (single symbol, both sides / a chosen side) ───────────────────

def _compute_result(d, symbol, side):
    """Score both style cards for each requested side and assemble the dual result. Shared by the
    dual endpoint and the detail endpoint (cc#408) so scores are identical by construction."""
    # cc#677 (founder-final, spec id=9035): ZERO-VETO. The verdict is SCORE BANDS ALONE — no gate ever
    # rejects. Every risk condition (F&O ban / result proximity / GVM floor / DTE) is an ALERT CHIP.
    # Both style cards are scored for each requested side; the best card by score sets the verdict.
    sides = (["BUY", "SELL"] if side == "ALL" else [side])
    cards = []
    for s in sides:
        for st in STYLES:
            cards.append(score_card(d, st, s))
    best = max(cards, key=lambda c: c["score"], default=None)
    return {
        "symbol": symbol, "cmp": _r(d["cmp"]), "side": side,
        "alerts": _alerts(d),                       # cc#677: informational chips only (never gate)
        "best": best,
        "best_label": best["label"] if best else None,
        "best_score": best["score"] if best else None,
        "best_verdict": (best["verdict"] if best else "REJECT"),   # score bands alone
        "cards": cards,
        "pivots": {k: _r(v) for k, v in d["pivots"].items()},
        "computed_at": _ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "spec_ref": SPEC_REF, "version": VERSION,
    }


def trade_check_v4_dual(symbol, side="ALL"):
    symbol = (symbol or "").strip().upper()
    side = (side or "ALL").strip().upper()
    if side not in ("BUY", "SELL", "ALL"):
        return {"error": f"side must be BUY, SELL or ALL, got {side!r}"}
    if not symbol:
        return {"error": "symbol required"}
    try:
        with psycopg.connect(_DB) as conn, conn.cursor() as cur:
            d = _load_one(cur, symbol)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    if not d["daily"]:
        return {"error": f"no raw_prices history for {symbol}"}
    if d["cmp"] is None:
        return {"error": f"no CMP available for {symbol}"}
    return _compute_result(d, symbol, side)


def _peer_rows(cur, segment, symbol, side):
    """Top same-segment peers by day% in the trade direction (for the R3 evidence panel)."""
    if not segment:
        return []
    order = "ASC" if side == "SELL" else "DESC"   # SELL wants the biggest fallers first
    # cc#453 fix_1: anchor peer day% to the last session that actually HAS day_1d (not a bare
    # MAX(score_date), which off-market can be a weekend GVM-nightly row with null day_1d -> every
    # peer row rendered "--"). This is the cc#424/#446 freeze-at-last-tick convention for peer day%.
    cur.execute(f"""
        SELECT g.symbol, v.day_1d
        FROM gvm_scores g JOIN v8_metrics v ON v.symbol = g.symbol
        WHERE g.segment = %s AND g.symbol <> %s
          AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
          AND v.score_date = (SELECT MAX(score_date) FROM v8_metrics WHERE day_1d IS NOT NULL)
          AND v.day_1d IS NOT NULL
        ORDER BY v.day_1d {order} LIMIT 8
    """, (segment, symbol))
    return [{"symbol": r[0], "day_1d": _r(r[1])} for r in cur.fetchall()]


def _oi_evidence(cur, d, v8):
    """cc#453 fix_5: R12 OI evidence — today's day%/OI-change + the OI/price quadrant tag (Long/Short
    Buildup · Short Covering · Long Unwinding) + the 5-day rolling OI strip. Reuses the Derivative
    Cockpit's deriv_metrics helpers so /check reads identically to the cockpit (cc#445 fix_3/fix_7)."""
    day_1d = v8.get("day_1d")
    oi_chg = d.get("oi_chg")
    blk = {"day_1d": _r(day_1d), "oi_chg": _r(oi_chg),
           "quadrant": None, "quadrant_color": None, "oi_5d": None}
    try:
        from deriv_metrics import _quadrant_tag, _oi_5d_rolling
        q = _quadrant_tag(oi_chg, day_1d)
        if q:
            blk["quadrant"] = q["label"]
            blk["quadrant_color"] = q["color"]
        blk["oi_5d"] = _oi_5d_rolling(cur, d.get("symbol"))
    except Exception:
        pass   # missing OI feed -> tag/strip simply absent; R12 still shows day%/OI-change
    return blk


def _evidence(cur, d, side):
    """Package already-computed rule inputs for the detail panels (cc#408). No new math."""
    v8 = d.get("v8") or {}
    bars = d.get("bars") or []
    return {
        "bars": [{"o": _r(b.get("open")), "h": _r(b.get("high")), "l": _r(b.get("low")),
                  "c": _r(b.get("close")), "v": _r(b.get("volume"))} for b in bars],
        "vwap": _r(d.get("vwap")), "range_pos": _r(d.get("range_pos")), "above_vwap": d.get("above_vwap"),
        "dma": {"d20": _r(v8.get("dma_20")), "d50": _r(v8.get("dma_50")), "d200": _r(v8.get("dma_200"))},
        "rsi": {"daily": _r(v8.get("daily_rsi")), "weekly": _r(v8.get("rsi_weekly")),
                "monthly": _r(v8.get("rsi_month")), "true_weekly": _r(d.get("true_weekly_rsi"))},
        "vol21": {"up_dn": _r(d.get("vol21_up_dn")), "dn_up": _r(d.get("vol21_dn_up")),
                  "au": _r(d.get("vol21_au")), "ad": _r(d.get("vol21_ad"))},
        "peers": _peer_rows(cur, d.get("segment"), d.get("symbol"), side),
        "peer_counts": {"up1": d.get("peers_up1"), "up": d.get("peers_up"),
                        "dn05": d.get("peers_dn05"), "dn": d.get("peers_dn"), "n": d.get("peer_count")},
        "sector": {"week": _r(v8.get("sector_week")), "month": _r(v8.get("sector_month"))},
        "rs": {"wk": _r(d.get("rs_wk")), "mo": _r(d.get("rs_mo"))},
        "returns": {"wk": _r(v8.get("week_return")), "mo": _r(v8.get("month_return"))},
        "style": {"mom_2d": _r(v8.get("mom_2d")), "week_index_52": _r(v8.get("week_index_52")),
                  "recovery_2d": _r(d.get("recovery_2d")), "fall_from_high_2d": _r(d.get("fall_from_high_2d"))},
        "oi": _oi_evidence(cur, d, v8),   # cc#453 fix_5: + quadrant tag + 5d rolling strip
        "basis": {"now": _r(d.get("basis_now")), "prev": _r(d.get("basis_prev")),
                  "fresh": bool(d.get("basis"))},
        "mood": {"fails": d.get("mood_fails"), "bull": d.get("mood_bull"), "adr": _r(d.get("adr")),
                 "nifty_day": _r(d.get("nifty_day")), "nifty_wk": _r(d.get("nifty_wk")),
                 "nifty_mo": _r(d.get("nifty_mo"))},
        "room": {"room_r1": _r(d.get("room_r1")), "room_s1": _r(d.get("room_s1")),
                 "above_pp": d.get("above_pp")},
        "fib": _fib_evidence(d),
        "gvm": _r(d.get("gvm_score")), "segment": d.get("segment"),
    }


def _fib_evidence(d):
    """cc#410: 3M fib swing + levels for the detail panel, with 6M-confluence MAJOR tags
    (a 3M level within ~1% of a 6M level). Display-only; the R16 credit is scored in _rules."""
    daily = d.get("daily") or []
    d3 = daily[-63:] if len(daily) >= 63 else daily
    d6 = daily[-126:] if len(daily) >= 126 else daily

    def _swing(rows):
        hs = [r["high"] for r in rows if r["high"] is not None]
        ls = [r["low"] for r in rows if r["low"] is not None]
        return (max(hs), min(ls)) if (hs and ls) else (None, None)

    h3, l3 = _swing(d3)
    h6, l6 = _swing(d6)
    lv3 = _fib_levels(h3, l3)
    lv6 = _fib_levels(h6, l6)
    for L in lv3:
        L["major"] = any(x["price"] and abs(L["price"] - x["price"]) / x["price"] <= 0.01 for x in lv6)
    return {"pos": _r(d.get("fib_pos")), "zone": _fib_zone_name(d.get("fib_pos")),
            "range_ok": d.get("fib_range_ok"), "swing_hi": _r(h3), "swing_lo": _r(l3),
            "cmp": _r(d.get("cmp")),
            # cc#453 fix_6: 3M closing-price path for the fib ZONE CHART (price line over the zone bands)
            "series": [_r(r["close"]) for r in d3 if r.get("close") is not None],
            "levels": [{"pct": x["pct"], "price": x["price"], "major": x["major"]} for x in lv3]}


def trade_check_v4_detail(symbol, side="BUY"):
    """cc#408: dual result + a full evidence block for the inline structure-style detail panels."""
    symbol = (symbol or "").strip().upper()
    side = (side or "BUY").strip().upper()
    if side == "ALL":
        side = "BUY"
    if side not in ("BUY", "SELL"):
        return {"error": f"side must be BUY or SELL, got {side!r}"}
    if not symbol:
        return {"error": "symbol required"}
    try:
        with psycopg.connect(_DB) as conn, conn.cursor() as cur:
            d = _load_one(cur, symbol)
            if not d["daily"]:
                return {"error": f"no raw_prices history for {symbol}"}
            if d["cmp"] is None:
                return {"error": f"no CMP available for {symbol}"}
            res = _compute_result(d, symbol, side)
            res["evidence"] = _evidence(cur, d, side)
            return res
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── routes ───────────────────────────────────────────────────────────────────────

class TCV4DualRequest(BaseModel):
    symbol: str
    side: str = "ALL"


@router.post("/api/trade-check/v4/dual")
def v4_dual_post(req: TCV4DualRequest):
    return trade_check_v4_dual(req.symbol, req.side)


@router.get("/api/trade-check/v4/dual")
def v4_dual_get(symbol: str, side: str = "ALL"):
    return trade_check_v4_dual(symbol, side)


@router.get("/api/trade-check/v4/detail")
def v4_detail_get(symbol: str, side: str = "BUY"):
    """cc#408: dual result + evidence block for the inline structure-style detail panels."""
    return trade_check_v4_detail(symbol, side)


@router.get("/api/trade-check/v4/health-dual")
def v4_dual_health():
    return {
        "version": VERSION, "spec_ref": SPEC_REF,
        "model": "dual-style: MOMENTUM + REVERSAL card per side, higher wins",
        "gates": "cc#677 ZERO-VETO — verdict = score bands alone; alert chips only (F&O ban · result-date · GVM floor · DTE)",
        "rules": "cc#767 V8-ALIGNED: R5 Volume-confirm (R5+R6 merged) · R12 Derivatives-confirm (R12+R13 merged) · R14 dropped; per-bucket V8 imports — BUY-MOM +dma_50/w52/GVM, BUY-REV mRSI+GVM+graded MAs, SELL-MOM CMP<PP+S2-clearance, SELL-REV graded 3-DMA+mom_2d/w52",
        # cc#934: no card's max is written down as a literal any more — it is SUMMED from each
        # rule's own declared max at score time, so this health block can only report the formula,
        # never a stale copy of a number that has drifted from the rulebook.
        "max_score": {"derivation": "sum(rule.max) over the rules that card actually emits",
                      "note": "no hardcoded per-card max exists; bands are ratio-derived from it",
                      # cc#935: computed live off the rulebook, not typed in. The old
                      # "example_BUY-MOM(max22)" line was a literal and had already gone stale.
                      "cards": card_maxes()},
        "verdict": {"ratios": {"STRONG": "0.84 x max", "VALID": "0.65 x max"}},
        "sides": {"BUY": "long", "SELL": "v4.1 mirror (GVM gate skipped)"},
        "status": "ok",
    }
