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

Verdict bands (founder-set): STRONG >= 12 | VALID 10 to <12 | REJECT < 10. Max 15.
SELL side = v4.1 mirror (locked same session): G1 GVM skipped (v3.3.2 short convention); all rules
mirrored per the spec's v4_1_sell_mirror table.

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

VERSION = "v4-dual.2-sell-recal"
SPEC_REF = "session_log id=2926 (v4 dual) + id=3010 (SELL recalibration, locked 12-Jul-2026)"

STYLES = ("MOMENTUM", "REVERSAL")
SIDES = ("BUY", "SELL")


def _ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


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


def _R(rid, label, credit, value):
    return {"rule": rid, "label": label, "credit": round(float(credit), 2), "value": value}


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

    # R15 — relative strength vs Nifty (stock return − nifty return), weekly & monthly
    wk, mo = v8.get("week_return"), v8.get("month_return")
    nwk, nmo = d.get("nifty_wk"), d.get("nifty_mo")
    d["rs_wk"] = (wk - nwk) if (wk is not None and nwk is not None) else None
    d["rs_mo"] = (mo - nmo) if (mo is not None and nmo is not None) else None

    # R11 — pivot location + room to the next level (%)
    piv = d.get("pivots") or {}
    pp, r1, s1 = piv.get("pp"), piv.get("r1"), piv.get("s1")
    d["above_pp"] = (cmp_v is not None and pp is not None and cmp_v > pp)
    d["room_r1"] = ((r1 - cmp_v) / cmp_v * 100.0) if (cmp_v and r1 is not None) else None
    d["room_s1"] = ((cmp_v - s1) / cmp_v * 100.0) if (cmp_v and s1 is not None) else None

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

    # DTE to monthly expiry (G3)
    today = _ist().date()
    exp = _current_expiry(today)
    d["dte"] = (exp - today).days
    return d


# ── data loader (single symbol) ──────────────────────────────────────────────────

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

    cur.execute("""SELECT gvm_score, segment FROM gvm_scores WHERE symbol=%s
                   ORDER BY score_date DESC LIMIT 1""", (symbol,))
    g = cur.fetchone()
    d["gvm_score"] = _f(g[0]) if g else None
    d["segment"] = g[1] if g else None

    d.update({"peers_up1": 0, "peers_up": 0, "peers_dn1": 0, "peers_dn05": 0, "peers_dn": 0, "peer_count": 0})
    if d["segment"]:
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE v.day_1d > 1),
                   COUNT(*) FILTER (WHERE v.day_1d > 0),
                   COUNT(*) FILTER (WHERE v.day_1d < -1),
                   COUNT(*) FILTER (WHERE v.day_1d < -0.5),
                   COUNT(*) FILTER (WHERE v.day_1d < 0),
                   COUNT(*)
            FROM gvm_scores g
            JOIN v8_metrics v ON v.symbol = g.symbol
            WHERE g.segment = %s AND g.symbol <> %s
              AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
              AND v.score_date = (SELECT MAX(score_date) FROM v8_metrics)
        """, (d["segment"], symbol))
        p = cur.fetchone()
        d.update({"peers_up1": int(p[0] or 0), "peers_up": int(p[1] or 0),
                  "peers_dn1": int(p[2] or 0), "peers_dn05": int(p[3] or 0),
                  "peers_dn": int(p[4] or 0), "peer_count": int(p[5] or 0)})

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

    cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
    a = cur.fetchone()
    d["adr"] = _f(a[0]) if a else None

    cur.execute("""SELECT basis_pct, oi_chg FROM futures_basis WHERE symbol=%s
                   ORDER BY ts DESC LIMIT 3""", (symbol,))
    d["basis"] = [{"basis_pct": _f(r[0]), "oi_chg": _f(r[1])} for r in cur.fetchall()]

    cur.execute("SELECT 1 FROM futures_universe WHERE UPPER(symbol)=UPPER(%s) AND is_active=TRUE", (symbol,))
    d["is_future"] = cur.fetchone() is not None

    cur.execute("""SELECT 1 FROM earnings_calendar
                   WHERE UPPER(ticker)=UPPER(%s) AND ex_date IN (CURRENT_DATE, CURRENT_DATE+1) LIMIT 1""",
                (symbol,))
    d["event_blackout"] = cur.fetchone() is not None

    return _derive(d)


# ── hard gates ───────────────────────────────────────────────────────────────────

def _gates(d, side):
    """3 hard gates. Any fail -> REJECT, no scorecard. G1 (GVM) skipped for SELL (short convention)."""
    gates = []
    ok = True
    if side == "BUY":
        g1 = (d.get("gvm_score") is not None and d["gvm_score"] >= 6.5)
        gates.append({"gate": "G1", "label": "GVM >= 6.5", "value": _r(d.get("gvm_score")), "pass": g1})
        ok = ok and g1
    else:
        gates.append({"gate": "G1", "label": "GVM n/a (short convention)", "value": None, "pass": True})
    g2 = not d.get("event_blackout")
    gates.append({"gate": "G2", "label": "Events clear (no ex-date T/T+1)", "value": d.get("event_blackout"), "pass": g2})
    g3 = bool(d.get("is_future")) and (d.get("dte") is not None and d["dte"] >= 3)
    gates.append({"gate": "G3", "label": "Futures & DTE >= 3", "value": d.get("dte"), "pass": g3})
    ok = ok and g2 and g3
    return ok, gates


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
        out.append(_R("R1", "Mood (fails)", 1.0 if f <= 1 else (0.5 if f == 2 else 0.0), f))
    else:
        # 3010: binary — any mood check bearish -> 1; all-bullish -> 0
        f = d.get("mood_fails", 0)
        out.append(_R("R1", "Mood (any bearish)", 1.0 if f >= 1 else 0.0, f))

    # R2 — sector (unchanged; already matches 3010)
    sw, sm = v8.get("sector_week"), v8.get("sector_month")
    if MOM:
        c = _both((sw or 0) > 0, (sm or 0) > 0) if BUY else _both((sw or 0) < 0, (sm or 0) < 0)
    else:
        pos = (sm is not None and (sm > 0 if BUY else sm < 0))
        c = 1.0 if pos else 0.0
    out.append(_R("R2", f"Sector {'wk&mo' if MOM else 'mo'}", c, {"wk": _r(sw), "mo": _r(sm)}))

    # R3 — peers in same segment (<3 peers -> auto 0.5). 3010 SELL MOM uses down>0.5% for the strong tier.
    if d.get("peer_count", 0) < 3:
        c = 0.5
    else:
        if BUY:
            strong, any_dir = d.get("peers_up1", 0), d.get("peers_up", 0)
        else:
            strong, any_dir = d.get("peers_dn05", 0), d.get("peers_dn", 0)
        if MOM:
            c = 1.0 if strong >= 2 else (0.5 if any_dir >= 1 else 0.0)
        else:
            c = 1.0 if any_dir >= 2 else (0.5 if any_dir >= 1 else 0.0)
    out.append(_R("R3", "Peers", c, {"strong": (d.get("peers_up1") if BUY else d.get("peers_dn05")),
                                     "dir": (d.get("peers_up") if BUY else d.get("peers_dn")),
                                     "n": d.get("peer_count")}))

    # R4 — moving averages (unchanged; matches 3010)
    above = [(v8.get("dma_20") or 0) > 0, (v8.get("dma_50") or 0) > 0, (v8.get("dma_200") or 0) > 0]
    below = [(v8.get("dma_20") or 0) < 0, (v8.get("dma_50") or 0) < 0, (v8.get("dma_200") or 0) < 0]
    if MOM:
        n = sum(above) if BUY else sum(below)
        c = 1.0 if n >= 2 else (0.5 if n == 1 else 0.0)
    else:
        d200 = v8.get("dma_200")
        c = 1.0 if (d200 is not None and (d200 > 0 if BUY else d200 < 0)) else 0.0
    out.append(_R("R4", f"MAs {'2of3' if MOM else 'DMA200'}", c,
                  {"d20": _r(v8.get("dma_20")), "d50": _r(v8.get("dma_50")), "d200": _r(v8.get("dma_200"))}))

    # R5 — 1-month up/down close volume ratio. BUY band (1.1,0.9); SELL relaxed to (1.05,0.95) per 3010.
    if BUY:
        ratio, c = d.get("vol21_up_dn"), _band(d.get("vol21_up_dn"), 1.1, 0.9)
    else:
        ratio, c = d.get("vol21_dn_up"), _band(d.get("vol21_dn_up"), 1.05, 0.95)
    out.append(_R("R5", "Vol 1M (up/dn)", c, _r(ratio)))

    # R6 — today's time-adjusted volume ratio. BUY only; DROPPED on SELL (3010).
    if BUY:
        out.append(_R("R6", "Vol today", _band(d.get("vol_ratio_today"), 1.5, 1.1), _r(d.get("vol_ratio_today"))))

    # R7 — RSI frame (MOM) / sandwich (REV)
    dr, mr, wr = v8.get("daily_rsi"), v8.get("rsi_month"), v8.get("rsi_weekly")
    twr = d.get("true_weekly_rsi")
    if MOM:
        if BUY:
            c = _both((mr or 0) >= 50, (wr or 0) >= 50)
        else:
            c = _both(wr is not None and wr < 50, mr is not None and mr < 50)   # 3010: wRSI<50 AND mRSI<50
        val = {"mRSI": _r(mr), "wRSI": _r(wr)}
    else:
        if BUY:
            c = _both((twr or 0) >= 60, (dr or 100) <= 40)
        else:
            c = _both(twr is not None and twr < 50, dr is not None and dr > 50)  # 3010 sandwich: wk<50 & daily>50
        val = {"trueWk": _r(twr), "dRSI": _r(dr)}
    out.append(_R("R7", f"RSI {'frame' if MOM else 'sandwich'}", c, val))

    # R8 — returns (unchanged; matches 3010)
    wk, mo = v8.get("week_return"), v8.get("month_return")
    if MOM:
        c = _both((wk or 0) > 0, (mo or 0) > 0) if BUY else _both((wk or 0) < 0, (mo or 0) < 0)
    else:
        c = 1.0 if (mo is not None and (mo > 0 if BUY else mo < 0)) else 0.0
    out.append(_R("R8", f"Returns {'wk&mo' if MOM else 'mo'}", c, {"wk": _r(wk), "mo": _r(mo)}))

    # R9 — 5-min structure + VWAP (session-anchored via last-session bars). 3010 SELL: below-VWAP is the
    #      gate for any credit — full (below-VWAP + weak structure) ->1, below-VWAP only ->0.5, else 0.
    upper = (d.get("range_pos") is not None and d["range_pos"] >= 0.5)
    lower = (d.get("range_pos") is not None and d["range_pos"] < 0.5)
    av = d.get("above_vwap")
    below_vwap = (av is False and d.get("vwap") is not None)
    if BUY:
        if MOM:
            c = _both(av, upper)
        else:
            c = _both((d.get("recovery_2d") or 0) > 0, av)
    else:
        second = lower if MOM else ((d.get("fall_from_high_2d") or 0) > 0)
        c = 1.0 if (below_vwap and second) else (0.5 if below_vwap else 0.0)
    out.append(_R("R9", "5m + VWAP", c, {"vwap": _r(d.get("vwap")), "rangePos": _r(d.get("range_pos"))}))

    # R10 — style extras
    m2 = v8.get("mom_2d")
    w52 = v8.get("week_index_52")
    if MOM:
        if BUY:
            c = _both(m2 is not None and 0 <= m2 <= 6, w52 is not None and 40 <= w52 <= 90)
        else:
            c = _both(m2 is not None and -6 <= m2 <= 0, w52 is not None and 10 <= w52 <= 60)
        val = {"mom_2d": _r(m2), "w52": _r(w52)}
    else:
        if BUY:
            rec = d.get("recovery_2d")
            c = 1.0 if (rec is not None and 2 <= rec <= 8) else 0.0
            val = {"recovery_2d": _r(rec)}
        else:
            # 3010: fall 2-8% ->1, 1-2% or 8-12% ->0.5, else 0
            fall = d.get("fall_from_high_2d")
            if fall is not None and 2 <= fall <= 8:
                c = 1.0
            elif fall is not None and ((1 <= fall < 2) or (8 < fall <= 12)):
                c = 0.5
            else:
                c = 0.0
            val = {"fall_2d": _r(fall)}
    out.append(_R("R10", "Style extra", c, val))

    # R11 — location + room. 3010 SELL MOM: below PP, S1-room >=2% ->1, 1-2% ->0.5, above PP ->0.
    #        SELL REV: S1-room >=2% ->1, 1-2% ->0.5, else 0.
    room_next = d.get("room_r1") if BUY else d.get("room_s1")
    if MOM:
        if BUY:
            if not d.get("above_pp"):
                c = 0.0
            elif room_next is not None and room_next >= 2:
                c = 1.0
            else:
                c = 0.5
        else:
            if d.get("above_pp"):
                c = 0.0
            elif room_next is not None and room_next >= 2:
                c = 1.0
            elif room_next is not None and room_next >= 1:
                c = 0.5
            else:
                c = 0.0
    else:
        if BUY:
            if room_next is None:
                c = 0.0
            elif room_next >= 3:
                c = 1.0
            elif room_next >= 2:
                c = 0.5
            else:
                c = 0.0
        else:
            if room_next is None:
                c = 0.0
            elif room_next >= 2:
                c = 1.0
            elif room_next >= 1:
                c = 0.5
            else:
                c = 0.0
    out.append(_R("R11", "Location + room", c, {"abovePP": d.get("above_pp"), "room": _r(room_next)}))

    # R12 — OI structure. 3010 SELL: short-buildup OR long-unwinding (price down) ->1; OI missing/stale ->0.5.
    day = v8.get("day_1d")
    oic = d.get("oi_chg")
    if BUY:
        c = 1.0 if (day is not None and day > 0 and oic is not None) else 0.0
    else:
        if oic is None:
            c = 0.5
        elif day is not None and day < 0:
            c = 1.0
        else:
            c = 0.0
    out.append(_R("R12", "OI structure", c, {"day": _r(day), "oi_chg": _r(oic)}))

    # R13 — basis. 3010 SELL: MOM discount widening ->1 / discount present ->0.5; REV premium fading ->1 /
    #        premium present (flat) ->0.5; basis missing ->0.5.
    now, prev = d.get("basis_now"), d.get("basis_prev")
    if BUY:
        if now is None or prev is None:
            c = 0.0
        elif MOM:
            c = 1.0 if (now > prev and now > 0) else 0.0
        else:
            c = 1.0 if (now > prev and now < 0) else 0.0
    else:
        if now is None or prev is None:
            c = 0.5
        elif MOM:
            c = 1.0 if (now < prev and now < 0) else (0.5 if now < 0 else 0.0)
        else:
            c = 1.0 if (now < prev and now > 0) else (0.5 if now > 0 else 0.0)
    out.append(_R("R13", "Basis", c, {"now": _r(now), "prev": _r(prev)}))

    # R14 — ATR ignition. BUY only; DROPPED on SELL (3010).
    if BUY:
        out.append(_R("R14", "ATR ignition", 1.0 if d.get("ignition") else 0.0,
                      {"tr": _r(d.get("tr_today")), "atr14": _r(d.get("atr14"))}))

    # R15 — relative strength vs Nifty (unchanged; matches 3010)
    rw, rm = d.get("rs_wk"), d.get("rs_mo")
    if MOM:
        c = _both((rw or 0) > 0, (rm or 0) > 0) if BUY else _both((rw or 0) < 0, (rm or 0) < 0)
        val = {"rs_wk": _r(rw), "rs_mo": _r(rm)}
    else:
        if rm is None:
            c = 0.0
        elif BUY:
            c = 1.0 if rm > 0 else (0.5 if -1 <= rm <= 0 else 0.0)
        else:
            c = 1.0 if rm < 0 else (0.5 if 0 <= rm <= 1 else 0.0)
        val = {"rs_mo": _r(rm)}
    out.append(_R("R15", "RS vs Nifty", c, val))

    return out


def _verdict(score, side="BUY"):
    # cc#400: SELL recalibrated (session_log id=3010) — max 13, separate bands. BUY unchanged (max 15).
    if side == "SELL":
        if score >= 10.5:
            return "STRONG"
        if score >= 8.5:
            return "VALID"
        return "REJECT"
    if score >= 12:
        return "STRONG"
    if score >= 10:
        return "VALID"
    return "REJECT"


def score_card(d, style, side):
    """The one shared scorer. Returns the full card for one (style, side).
    cc#400: SELL drops R6+R14 (max 13) and uses its own verdict bands; BUY untouched (max 15)."""
    rules = _rules(d, style, side)
    score = round(sum(r["credit"] for r in rules), 2)
    max_score = 13 if side == "SELL" else 15
    card = {"style": style, "side": side, "label": f"{side}-{style[:3]}",
            "score": score, "max": max_score, "verdict": _verdict(score, side), "rules": rules}
    if side == "SELL":
        card["recal"] = "RECALIBRATED 12-JUL"
        card["bands"] = "STRONG≥10.5 / VALID 8.5–10.5 / REJECT<8.5"
    else:
        card["bands"] = "STRONG≥12 / VALID 10–12 / REJECT<10"
    return card


# ── public compute (single symbol, both sides / a chosen side) ───────────────────

def _compute_result(d, symbol, side):
    """Score both style cards for each requested side and assemble the dual result. Shared by the
    dual endpoint and the detail endpoint (cc#408) so scores are identical by construction."""
    # cc#402: gates keep verdict authority but never hide information — ALWAYS compute both style
    # cards for each requested side, flag the gated ones, and let the caller render them with a
    # GATED banner. Overall verdict stays REJECT whenever the best card's side gates failed.
    sides = (["BUY", "SELL"] if side == "ALL" else [side])
    cards, gate_map = [], {}
    for s in sides:
        ok, gates = _gates(d, s)
        fails = [g for g in gates if not g.get("pass")]
        gate_map[s] = {"pass": ok, "gates": gates, "fails": fails}
        for st in STYLES:
            card = score_card(d, st, s)
            card["gated"] = (not ok)
            card["gate_fails"] = fails
            cards.append(card)
    passing = [c for c in cards if not c["gated"]]
    pool = passing if passing else cards
    best = max(pool, key=lambda c: c["score"], default=None)
    best_gated = bool(best and best.get("gated"))
    return {
        "symbol": symbol, "cmp": _r(d["cmp"]), "side": side,
        "gates": gate_map,
        "best": best,
        "best_label": best["label"] if best else None,
        "best_score": best["score"] if best else None,
        "best_verdict": ("REJECT" if best_gated else (best["verdict"] if best else "REJECT")),
        "gated": best_gated,
        "gate_fails": (best.get("gate_fails") if best else []),
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
    cur.execute(f"""
        SELECT g.symbol, v.day_1d
        FROM gvm_scores g JOIN v8_metrics v ON v.symbol = g.symbol
        WHERE g.segment = %s AND g.symbol <> %s
          AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
          AND v.score_date = (SELECT MAX(score_date) FROM v8_metrics)
          AND v.day_1d IS NOT NULL
        ORDER BY v.day_1d {order} LIMIT 8
    """, (segment, symbol))
    return [{"symbol": r[0], "day_1d": _r(r[1])} for r in cur.fetchall()]


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
        "oi": {"day_1d": _r(v8.get("day_1d")), "oi_chg": _r(d.get("oi_chg"))},
        "basis": {"now": _r(d.get("basis_now")), "prev": _r(d.get("basis_prev")),
                  "fresh": bool(d.get("basis"))},
        "mood": {"fails": d.get("mood_fails"), "bull": d.get("mood_bull"), "adr": _r(d.get("adr")),
                 "nifty_day": _r(d.get("nifty_day")), "nifty_wk": _r(d.get("nifty_wk")),
                 "nifty_mo": _r(d.get("nifty_mo"))},
        "room": {"room_r1": _r(d.get("room_r1")), "room_s1": _r(d.get("room_s1")),
                 "above_pp": d.get("above_pp")},
        "gvm": _r(d.get("gvm_score")), "segment": d.get("segment"),
    }


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
        "gates": "G1 GVM>=6.5 (BUY only) · G2 events clear · G3 futures & DTE>=3",
        "rules": 15, "max_score": 15,
        "verdict": {"STRONG": ">=12", "VALID": "10 to <12", "REJECT": "<10"},
        "sides": {"BUY": "long", "SELL": "v4.1 mirror (GVM gate skipped)"},
        "status": "ok",
    }
