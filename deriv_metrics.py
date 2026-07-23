"""
deriv_metrics.py — cc#346 DERIVATIVE COCKPIT data layer.

GET /api/deriv-metrics/{symbol} -> ONE JSON with the per-symbol derivative read the
founder watches daily on open positions. Shared component keyed purely by symbol (later
mountable from GVM / Trade Check). Everything is computed from EXISTING tables — zero new
feeds. Any section with no data returns null so the UI (cc#347) hides that row.

Sections (round-2 additions dropped per founder 09-Jul):
  VERDICT  — OI Quadrant (fut OI chg x price chg) + Options-Cost (IV/RV, upgrades to IVP)
  LEVELS   — VPOC today + prior-day naked, OI walls (ATM+-3), VWAP distance
  FLOW     — fut OI d/d, call/put OI d/d + PCR + day chg, basis + 5d percentile/spark
  ENERGY   — VolX (time-matched vol multiple), hourly %, fall-from-day-high
  ATM      — CE/PE ltp+bid/ask, market straddle vs BS fair (sigma=RV20) -> premium% gap chip

atm_iv_daily(symbol,d,atm_iv,rv20,straddle_pct) is ensured here; a 15:25 snapshot job fills
it. Once a symbol has >=60 rows the Options-Cost verdict auto-upgrades from IV/RV to true IVP.
"""
import os, math, time, logging
from datetime import date
from typing import Optional, Dict, Any, List

import psycopg
from fastapi import APIRouter, HTTPException

log = logging.getLogger("scorr.deriv")
deriv_router = APIRouter(tags=["deriv"])
DATABASE_URL = os.getenv("DATABASE_URL", "")
R_FREE = 0.07          # risk-free rate for Black-Scholes
ATM_BAND = 3           # ATM +- N strikes = the tracked stock-option band
IVP_MIN_ROWS = 60      # atm_iv_daily rows needed before IVP replaces IV/RV


def _conn():
    return psycopg.connect(DATABASE_URL)


def _f(x) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _safe(label, fn, default=None):
    """cc#449: per-field graceful degradation. Any single cockpit field that throws (a symbol
    lacking some datum, a null in a join) falls back to `default` and logs — it NEVER kills the
    whole panel. The response is assembled from these so one bad field can't 500 the cockpit."""
    try:
        return fn()
    except Exception as e:
        log.warning(f"deriv field '{label}' degraded: {e}")
        return default


# cc#348: TC Score chip — computed on sheet-open via the SAME engine the /check page uses
# (native_trade_check.compute_trade_check), cached 5 min per (symbol, side). Never reads the
# stale tc_cache / tc_screener_cache. verdict_class pass/watch/fail -> STRONG/VALID/WEAK.
_TC_CACHE: Dict[tuple, tuple] = {}
_TC_TTL = 300.0


def _tc_score(sym: str, side: Optional[str]) -> Optional[Dict[str, Any]]:
    # cc#427 fix_3: source = TC v4 dual HIGHEST card. Returns the winning style tag (e.g. SELL-REV),
    # the trade side, side colour (cc#405: BUY=bull/green, SELL=bear/red), the verdict band, and the
    # score as X/16 (BUY) or X/14 (SELL). When a position side is known (LONG->BUY, SHORT->SELL) we
    # score that side; otherwise "ALL" and the engine's best card wins.
    v4side = {"LONG": "BUY", "SHORT": "SELL"}.get((side or "").upper(), "ALL")
    key = (sym, v4side)
    now = time.time()
    c = _TC_CACHE.get(key)
    if c and now - c[0] < _TC_TTL:
        return c[1]
    res = None
    try:
        from tc_v4_dual import trade_check_v4_dual
        r = trade_check_v4_dual(sym, v4side)
        b = r.get("best") if (r and not r.get("error")) else None
        if b:
            bside = b.get("side")
            col = "bull" if bside == "BUY" else "bear"           # cc#405 side palette
            verdict = "REJECT" if r.get("gated") else b.get("verdict")
            res = {"score": b.get("score"), "total": b.get("max"),
                   "style": (b.get("label") or "").upper(),      # "SELL-REV" / "BUY-MOM"
                   "side": bside, "verdict": verdict, "label": verdict, "color": col,
                   "gated": bool(r.get("gated"))}
    except Exception as e:
        log.warning(f"tc_score {sym}/{side}: {e}")
    _TC_CACHE[key] = (now, res)
    return res


# ── Black-Scholes: price + IV inversion (bisection) ─────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S, K, T, sigma, cp) -> Optional[float]:
    if not (S and K and T and sigma) or S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    d1 = (math.log(S / K) + (R_FREE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if cp == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-R_FREE * T) * _norm_cdf(d2)
    return K * math.exp(-R_FREE * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_iv(price, S, K, T, cp) -> Optional[float]:
    """Implied vol by bisection on [0.01%, 500%]. None if price is below intrinsic / bad input."""
    if not (price and S and K and T) or price <= 0 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(64):
        mid = (lo + hi) / 2.0
        p = _bs_price(S, K, T, mid, cp)
        if p is None:
            return None
        if p > price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2.0, 4)


# ── section builders ───────────────────────────────────────────────────────────
def _ensure_iv_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS atm_iv_daily (
            symbol TEXT NOT NULL, d DATE NOT NULL,
            atm_iv NUMERIC, rv20 NUMERIC, straddle_pct NUMERIC,
            PRIMARY KEY (symbol, d)
        )""")


def _basis_block(cur, sym) -> Dict[str, Any]:
    cur.execute("""SELECT basis, basis_pct, futures_close, spot_close, oi, oi_chg, ts
                   FROM futures_basis WHERE symbol=%s ORDER BY ts DESC LIMIT 1""", (sym,))
    r = cur.fetchone()
    if not r:
        return {}
    basis, basis_pct, fut, spot, oi, oi_chg = (_f(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]))
    # 5-day basis trend (one latest value per day) -> spark + percentile of latest in its range
    cur.execute("""SELECT DISTINCT ON (ts::date) ts::date, basis
                   FROM futures_basis WHERE symbol=%s AND basis IS NOT NULL
                   ORDER BY ts::date DESC, ts DESC LIMIT 5""", (sym,))
    spark = [_f(x[1]) for x in cur.fetchall()][::-1]
    pct = None
    if spark and len(spark) >= 2 and basis is not None:
        lo, hi = min(spark), max(spark)
        pct = round((basis - lo) / (hi - lo) * 100.0, 1) if hi > lo else 50.0
    # cc#374: honest day-over-day OI %. The stored oi_chg is an ABSOLUTE contract delta (BIGINT,
    # bar-over-bar) that the cockpit rendered with a '%' sign -> "-1500.0%" garbage. Recompute a real
    # d/d percent: latest OI vs the last OI of the PRIOR trading session; None (-> "--") if missing.
    cur.execute("""SELECT oi FROM futures_basis
                   WHERE symbol=%s AND oi IS NOT NULL
                     AND ts::date < (SELECT MAX(ts::date) FROM futures_basis WHERE symbol=%s AND oi IS NOT NULL)
                   ORDER BY ts DESC LIMIT 1""", (sym, sym))
    _pr = cur.fetchone()
    oi_prev_day = _f(_pr[0]) if _pr else None
    oi_dd_pct = (round((oi - oi_prev_day) / oi_prev_day * 100.0, 1)
                 if (oi is not None and oi_prev_day and oi_prev_day > 0) else None)
    # cc#515: OI-staleness honesty. futures_basis.oi has been observed NULL for several sessions
    # (e.g. MOTHERSON since 15-Jul) while the quadrant tile just showed bare "DATA THIN" -- surface
    # the actual staleness date so it reads as a known feed gap, not a silent degrade.
    cur.execute("SELECT MAX(ts::date) FROM futures_basis WHERE symbol=%s", (sym,))
    _ld = cur.fetchone()
    latest_session = _ld[0] if _ld else None
    cur.execute("SELECT MAX(ts::date) FROM futures_basis WHERE symbol=%s AND oi IS NOT NULL", (sym,))
    _od = cur.fetchone()
    oi_last_session = _od[0] if _od else None
    oi_stale = (oi_last_session is None) or (latest_session is not None and oi_last_session < latest_session)
    # cc#515: today's intraday basis trend (first tick of the session vs the latest/current tick) --
    # drives the composite READ's basis-direction vote ("premium widening/fading" language describes
    # movement DURING a session, not the 5-day daily spark used for the percentile tag above).
    cur.execute("""SELECT basis_pct FROM futures_basis WHERE symbol=%s
                   AND ts::date = (SELECT MAX(ts::date) FROM futures_basis WHERE symbol=%s)
                   ORDER BY ts ASC LIMIT 1""", (sym, sym))
    _r0 = cur.fetchone()
    basis_open_pct = _f(_r0[0]) if _r0 else None
    _btag, _btag_color = _basis_tag(pct, (basis is None or basis >= 0))   # cc#445 fix_2 / cc#624 item_2
    return {"fut": fut, "spot": spot,
            "basis": {"value": basis, "pct": basis_pct, "percentile": pct, "spark": spark,
                      "intraday_open_pct": basis_open_pct,
                      "tag": _btag, "tag_color": _btag_color},
            "fut_oi": {"oi": oi, "chg_pct": oi_dd_pct, "stale": oi_stale,
                       "stale_since": str(oi_last_session) if oi_last_session else None}}


def _metrics(cur, sym) -> Dict[str, Any]:
    cur.execute("""SELECT day_1d, daily_rsi, rsi_weekly FROM v8_metrics WHERE symbol=%s
                   ORDER BY score_date DESC LIMIT 1""", (sym,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"price_chg": _f(r[0]), "rsi_d": _f(r[1]), "rsi_w": _f(r[2])}


def _oi_quadrant(oi_chg, price_chg) -> Optional[Dict[str, Any]]:
    # cc#375: INPUTS ARE FUTURES OI ONLY — oi_chg is the FUT OI day-over-day % (futures_basis, honest
    # since cc#374), price_chg is day_1d. This is the classic futures OI/price map (Long/Short Buildup,
    # Long Unwind, Short Covering); options OI is NOT an input (it lives in the separate ATM Call/Put OI
    # rows). Returns None -> the tile shows "DATA THIN" whenever either input is missing, so it never
    # renders a confident label off absent/garbage OI (a stock with no fut OI d/d stays DATA THIN).
    if oi_chg is None or price_chg is None:
        return None
    oi_up, px_up = oi_chg > 0, price_chg > 0
    if oi_up and px_up:
        label, color = "LONG BUILDUP", "bull"
    elif oi_up and not px_up:
        label, color = "SHORT BUILDUP", "bear"
    elif not oi_up and not px_up:
        label, color = "LONG UNWIND", "bear"
    else:
        label, color = "SHORT COVERING", "bull"
    return {"label": label, "color": color, "oi_chg_pct": oi_chg, "price_chg_pct": price_chg}


def _oi_5d_rolling(cur, sym) -> Optional[Dict[str, Any]]:
    """cc#427/445, cc#621 issue 1: last-5-session futures OI d/d %, each labelled with its DATE and
    tagged with the OI/price quadrant. GAP-TOLERANT: select the last 6 DISTINCT trading dates PRESENT
    in futures_basis (NOT gated on oi-not-null), so a missing/thin session — e.g. the 22-Jul fut-only
    outage, or a null-OI day (cc#515) — no longer collapses the table. Each row's OI Δ% is computed vs
    the previous AVAILABLE OI (carry-forward across gap/null days); the row still renders with a null Δ
    when its own OI is missing. Up to 5 rows; Net = sum over rendered rows."""
    cur.execute("""SELECT DISTINCT ON (ts::date) ts::date, oi FROM futures_basis
                   WHERE symbol=%s ORDER BY ts::date DESC, ts DESC LIMIT 6""", (sym,))
    rows = [(r[0], _f(r[1])) for r in cur.fetchall()][::-1]   # oldest -> newest (present dates)
    if len(rows) < 2:
        return None
    # daily closes for the same dates (for price-change -> quadrant)
    cur.execute("""SELECT price_date, close FROM raw_prices WHERE symbol=%s
                   AND price_date <= %s ORDER BY price_date DESC LIMIT 7""", (sym, rows[-1][0]))
    closes = {r[0]: _f(r[1]) for r in cur.fetchall()}
    _cdates = sorted(closes.keys())

    def _px_chg(d):
        if d not in closes:
            return None
        idx = _cdates.index(d) if d in _cdates else -1
        if idx <= 0:
            return None
        prevc = closes.get(_cdates[idx - 1])
        c = closes.get(d)
        return round((c - prevc) / prevc * 100.0, 2) if (prevc and c) else None

    series = []
    last_oi = rows[0][1]   # previous AVAILABLE OI (carry-forward base); may be None if the oldest is null
    for i in range(1, len(rows)):
        d, oi = rows[i]
        pct = (round((oi - last_oi) / last_oi * 100.0, 1)
               if (oi is not None and last_oi and last_oi > 0) else None)
        px = _px_chg(d)
        q = _quadrant_tag(pct, px)
        series.append({"date": str(d), "chg_pct": pct, "px_chg": px,
                       "quadrant": q["label"] if q else None, "quadrant_color": q["color"] if q else None})
        if oi is not None:
            last_oi = oi   # advance the base only on an available OI (gap-tolerant pairing)
    series = series[-5:]
    vals = [s["chg_pct"] for s in series if s["chg_pct"] is not None]
    net = round(sum(vals), 1) if vals else None
    return {"series": series, "net_pct": net}


def _chain_latest_two_days(cur, sym):
    """Latest option_chain snapshot dates (today + prior). option_chain is keyed by `underlying`
    (the .symbol column holds the full contract code, e.g. NSE:ALKEM26JUL...)."""
    cur.execute("SELECT DISTINCT ts::date FROM option_chain WHERE underlying=%s ORDER BY ts::date DESC LIMIT 2", (sym,))
    return [x[0] for x in cur.fetchall()]


def _chain_rows(cur, sym, d):
    """All rows at the latest ts on date d for this underlying."""
    cur.execute("""SELECT strike, option_type, ltp, oi, bid, ask, expiry FROM option_chain
                   WHERE underlying=%s AND ts = (SELECT MAX(ts) FROM option_chain WHERE underlying=%s AND ts::date=%s)""",
                (sym, sym, d))
    return cur.fetchall()


def _options_block(cur, sym, cmp_px) -> Dict[str, Any]:
    """ATM+-3 OI (call/put + PCR + d/d), OI walls, ATM CE/PE, IV/RV, BS fair-value gap."""
    days = _chain_latest_two_days(cur, sym)
    if not days or cmp_px is None:
        return {"has_options": False}
    rows = _chain_rows(cur, sym, days[0])
    if not rows:
        return {"has_options": False}

    strikes = sorted({_f(r[0]) for r in rows if r[0] is not None})
    if not strikes:
        return {"has_options": False}
    atm = min(strikes, key=lambda k: abs(k - cmp_px))
    ai = strikes.index(atm)
    band = set(strikes[max(0, ai - ATM_BAND): ai + ATM_BAND + 1])

    def _sum_oi(rws, cp):
        # None (not 0) when the chain carries no OI for this side/band — so the UI hides the row
        # rather than showing a misleading zero (stock-option OI is often unpopulated).
        vals = [int(r[3]) for r in rws if r[1] == cp and _f(r[0]) in band and r[3] is not None]
        return sum(vals) if vals else None

    call_oi, put_oi = _sum_oi(rows, "CE"), _sum_oi(rows, "PE")
    # d/d change vs prior chain day
    call_chg = put_chg = pcr_chg = None
    prior_pcr = None
    if len(days) > 1:
        prows = _chain_rows(cur, sym, days[1])
        if prows:
            pc, pp = _sum_oi(prows, "CE"), _sum_oi(prows, "PE")
            if call_oi is not None and pc:
                call_chg = round((call_oi - pc) / pc * 100.0, 1)
            if put_oi is not None and pp:
                put_chg = round((put_oi - pp) / pp * 100.0, 1)
            prior_pcr = (pp / pc) if (pc and pp) else None
    pcr = round(put_oi / call_oi, 2) if (call_oi and put_oi is not None) else None
    if pcr is not None and prior_pcr:
        pcr_chg = round(pcr - prior_pcr, 2)

    # OI walls within the tracked band: highest-OI call strike > CMP, put strike < CMP.
    # Only meaningful when the chain carries OI — else hidden.
    def _wall_row(cp, above):
        cands = [r for r in rows if r[1] == cp and r[3] is not None and _f(r[0]) in band
                 and _f(r[0]) is not None and ((_f(r[0]) > cmp_px) if above else (_f(r[0]) < cmp_px))]
        return max(cands, key=lambda r: int(r[3]), default=None) if cands else None
    call_wall = _wall_row("CE", True)
    put_wall = _wall_row("PE", False)

    def _wall(r):
        if not r:
            return None
        k = _f(r[0])
        return {"strike": k, "oi": int(r[3] or 0), "dist_pct": round((k - cmp_px) / cmp_px * 100.0, 2)}

    # ATM CE/PE quote
    ce = next((r for r in rows if r[1] == "CE" and _f(r[0]) == atm), None)
    pe = next((r for r in rows if r[1] == "PE" and _f(r[0]) == atm), None)
    ce_ltp, pe_ltp = (_f(ce[2]) if ce else None), (_f(pe[2]) if pe else None)
    expiry = (ce[6] if ce else (pe[6] if pe else None))
    T = None
    if expiry is not None:
        T = max((expiry - date.today()).days, 0) / 365.0
        if T <= 0:
            T = 0.5 / 365.0     # expiry-day floor so BS/IV stay finite

    # ATM IV (avg of CE/PE inversions) using spot=CMP
    iv_ce = _bs_iv(ce_ltp, cmp_px, atm, T, "CE")
    iv_pe = _bs_iv(pe_ltp, cmp_px, atm, T, "PE")
    ivs = [v for v in (iv_ce, iv_pe) if v is not None]
    atm_iv = round(sum(ivs) / len(ivs), 4) if ivs else None

    # RV20 (annualized) from raw_prices closes
    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND close IS NOT NULL
                   ORDER BY price_date DESC LIMIT 21""", (sym,))
    closes = [_f(x[0]) for x in cur.fetchall()][::-1]
    rv20 = None
    if len(closes) >= 21:
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1]]
        if rets:
            m = sum(rets) / len(rets)
            var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
            rv20 = round(math.sqrt(var) * math.sqrt(252.0), 4)

    # Options-cost verdict: IVP once >=60 diary rows exist, else IV/RV
    cost = None
    cur.execute("SELECT COUNT(*) FROM atm_iv_daily WHERE symbol=%s", (sym,))
    n_diary = int(cur.fetchone()[0] or 0)
    if n_diary >= IVP_MIN_ROWS and atm_iv is not None:
        cur.execute("""SELECT (COUNT(*) FILTER (WHERE atm_iv <= %s))::float / NULLIF(COUNT(*),0) * 100
                       FROM atm_iv_daily WHERE symbol=%s AND atm_iv IS NOT NULL""", (atm_iv, sym))
        ivp = _f(cur.fetchone()[0])
        if ivp is not None:
            ivp = round(ivp, 0)
            lbl, col = ("EXPENSIVE", "bear") if ivp > 70 else ("CHEAP", "bull") if ivp < 30 else ("REASONABLE", "amber")
            cost = {"basis": "IVP", "ivp": ivp, "iv": atm_iv, "rv20": rv20, "label": lbl, "color": col}
    if cost is None and atm_iv is not None and rv20:
        ratio = round(atm_iv / rv20, 2)
        lbl, col = ("EXPENSIVE", "bear") if ratio > 1.3 else ("CHEAP", "bull") if ratio < 0.9 else ("REASONABLE", "amber")
        cost = {"basis": "IV/RV", "iv_rv": ratio, "iv": atm_iv, "rv20": rv20, "label": lbl, "color": col}

    # BS fair-value straddle gap (sigma = RV20) — founder-final bands
    gap = None
    fair_ce = fair_pe = None
    market_str = (ce_ltp or 0) + (pe_ltp or 0) if (ce_ltp is not None and pe_ltp is not None) else None
    if market_str and rv20 and T:
        fair_ce = _bs_price(cmp_px, atm, T, rv20, "CE")
        fair_pe = _bs_price(cmp_px, atm, T, rv20, "PE")
        if fair_ce and fair_pe:
            fair = fair_ce + fair_pe
            prem = round((market_str - fair) / fair * 100.0, 1)
            lbl, col = ("EXPENSIVE", "bear") if prem > 25 else ("CHEAP", "bull") if prem < 0 else ("REASONABLE", "amber")
            gap = {"market": round(market_str, 2), "fair": round(fair, 2), "premium_pct": prem,
                   "label": lbl, "color": col}
    # cc#427 fix_2: the OPTIONS COST verdict's "working" — ATM strike + current ATM-CE premium vs the
    # model (BS, sigma=RV20) fair premium and their ratio, e.g. "2920 CE @ 41 vs fair 78 · 0.53x".
    if cost is not None and ce_ltp is not None and fair_ce:
        cost["working"] = {"strike": atm, "ce_ltp": round(ce_ltp, 2),
                           "ce_fair": round(fair_ce, 2),
                           "ce_ratio": round(ce_ltp / fair_ce, 2) if fair_ce else None}

    # cc#348: single-ATM strike OI d/d (Call + Put) — the OI trio rows 5 & 6. Each side at the
    # ATM strike; strike labelled. Stock-option OI is often unfed -> chg stays null (row shows --).
    def _strike_oi(rws, cp):
        x = next((r for r in rws if r[1] == cp and _f(r[0]) == atm and r[3] is not None), None)
        return int(x[3]) if x else None
    atm_call_oi, atm_put_oi = _strike_oi(rows, "CE"), _strike_oi(rows, "PE")
    atm_call_chg = atm_put_chg = None
    if len(days) > 1:
        prows2 = _chain_rows(cur, sym, days[1])
        if prows2:
            pca, ppa = _strike_oi(prows2, "CE"), _strike_oi(prows2, "PE")
            if atm_call_oi is not None and pca:
                atm_call_chg = round((atm_call_oi - pca) / pca * 100.0, 1)
            if atm_put_oi is not None and ppa:
                atm_put_chg = round((atm_put_oi - ppa) / ppa * 100.0, 1)

    # cc#427 fix_6: distinguish "only one snapshot exists yet" from genuinely-missing OI, so the UI
    # can show "1st snapshot" (not a bare "--") when there is current OI but no prior day to diff against.
    _first_snap = len(days) < 2
    # cc#454: the fyers option-chain feed populates OI for INDEX options only (NIFTY/BANKNIFTY); every
    # single-STOCK chain carries LTP but a NULL oi column. Flag that so the cockpit shows an honest
    # "OI n/f" (not fed) instead of a bare "--" that reads like a broken fix. The chain-based d/d is
    # correct — there is simply no stock-option OI in the feed to diff.
    _oi_unfed = (call_oi is None and put_oi is None and atm_call_oi is None and atm_put_oi is None)
    return {
        "has_options": True,
        "atm_call_oi": {"strike": atm, "oi": atm_call_oi, "chg_pct": atm_call_chg, "first_snapshot": _first_snap, "oi_unfed": _oi_unfed},
        "atm_put_oi": {"strike": atm, "oi": atm_put_oi, "chg_pct": atm_put_chg, "first_snapshot": _first_snap, "oi_unfed": _oi_unfed},
        "call_oi": {"oi": call_oi, "chg_pct": call_chg},
        "put_oi": {"oi": put_oi, "chg_pct": put_chg},
        "pcr": {"value": pcr, "chg": pcr_chg},
        "oi_walls": {"call": _wall(call_wall), "put": _wall(put_wall)},
        "atm": {
            "strike": atm,
            "ce_ltp": ce_ltp, "ce_bid": _f(ce[4]) if ce else None, "ce_ask": _f(ce[5]) if ce else None,
            "pe_ltp": pe_ltp, "pe_bid": _f(pe[4]) if pe else None, "pe_ask": _f(pe[5]) if pe else None,
            "gap": gap, "expiry": str(expiry) if expiry else None,   # cc#516: options-cost meaning line needs this
        },
        "options_cost": cost,
    }


def _intraday_block(cur, sym, cmp_px) -> Dict[str, Any]:
    """VPOC (today + prior naked), VWAP distance, VolX, hourly %, fall-from-day-high — from fyers 5m."""
    out: Dict[str, Any] = {}

    # today's & prior day's fyers_eq 5-min bars
    cur.execute("SELECT DISTINCT ts::date FROM intraday_prices WHERE symbol=%s AND source='fyers_eq' ORDER BY ts::date DESC LIMIT 6", (sym,))
    days = [x[0] for x in cur.fetchall()]
    if not days:
        return out
    today = days[0]

    def _bars(d):
        cur.execute("""SELECT close, high, low, volume, ts FROM intraday_prices
                       WHERE symbol=%s AND source='fyers_eq' AND ts::date=%s AND ts::time>='09:15:00'
                       ORDER BY ts ASC""", (sym, d))
        return cur.fetchall()

    def _vpoc(bars):
        buckets: Dict[int, float] = {}
        for c, h, l, v, ts in bars:
            c = _f(c); v = _f(v) or 0
            if c is None:
                continue
            b = int(round(c))
            buckets[b] = buckets.get(b, 0.0) + v
        if not buckets:
            return None
        return max(buckets, key=buckets.get)

    tb = _bars(today)
    if tb:
        vpoc_t = _vpoc(tb)
        hi = max((_f(x[1]) for x in tb if _f(x[1]) is not None), default=None)
        lo = min((_f(x[2]) for x in tb if _f(x[2]) is not None), default=None)
        out["vpoc_today"] = {"value": vpoc_t,
                             "dist_pct": round((cmp_px - vpoc_t) / vpoc_t * 100.0, 2) if (vpoc_t and cmp_px) else None}
        # VWAP
        num = sum((_f(x[0]) or 0) * (_f(x[3]) or 0) for x in tb)
        den = sum((_f(x[3]) or 0) for x in tb)
        if den:
            vwap = num / den
            out["vwap"] = {"value": round(vwap, 2),
                           "dist_pct": round((cmp_px - vwap) / vwap * 100.0, 2) if cmp_px else None}
        # fall-from-day-high. cc#516: day_hi kept for the interpretation-layer line ("rallied to X,
        # faded -Y% into close").
        out["day_hi"] = hi
        if hi and cmp_px:
            out["fall_from_day_high"] = round((cmp_px - hi) / hi * 100.0, 2)
        # cc#516: today's daily true range (vs the prior session's close) -- compared to the daily
        # ATR14 (computed separately) for the ATR tile's "compressed range / coiling" and "ignition"
        # interpretation lines.
        if hi is not None and lo is not None:
            cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND close IS NOT NULL
                           AND price_date < %s ORDER BY price_date DESC LIMIT 1""", (sym, today))
            _pc = cur.fetchone()
            prev_close = _f(_pc[0]) if _pc else None
            out["tr_today"] = round(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)), 2) \
                if prev_close is not None else round(hi - lo, 2)
        # cc#445 fix_8: opening range (09:15-09:30) + CMP position vs range (off-market = last session)
        _orb = [x for x in tb if x[4].time().hour == 9 and x[4].time().minute < 30]
        or_hi = max((_f(x[1]) for x in _orb if _f(x[1]) is not None), default=None)
        or_lo = min((_f(x[2]) for x in _orb if _f(x[2]) is not None), default=None)
        if or_hi is not None and or_lo is not None and cmp_px:
            if cmp_px > or_hi:
                pos, edge = "Above", round((cmp_px - or_hi) / or_hi * 100.0, 2)
            elif cmp_px < or_lo:
                pos, edge = "Below", round((or_lo - cmp_px) / or_lo * 100.0, 2)
            else:
                pos = "Inside"
                edge = round(min(abs(cmp_px - or_hi), abs(cmp_px - or_lo)) / cmp_px * 100.0, 2)
            out["orb"] = {"high": round(or_hi, 2), "low": round(or_lo, 2), "position": pos,
                          "edge_pct": edge, "session": str(today)}
        # cc#445 fix_6: intraday ATR(5m, 20)
        _trs = []
        for i in range(1, len(tb)):
            h2 = _f(tb[i][1]); l2 = _f(tb[i][2]); pc2 = _f(tb[i - 1][0])
            if None in (h2, l2, pc2):
                continue
            _trs.append(max(h2 - l2, abs(h2 - pc2), abs(l2 - pc2)))
        if _trs:
            _atr5 = sum(_trs[-20:]) / len(_trs[-20:])
            out["atr_5m"] = round(_atr5, 2)
            out["atr_5m_pct"] = round(_atr5 / cmp_px * 100.0, 2) if cmp_px else None
        # prior-day naked VPOC (prior VPOC untouched by today's range)
        if len(days) > 1:
            pb = _bars(days[1])
            vpoc_p = _vpoc(pb)
            if vpoc_p is not None:
                naked = not (lo is not None and hi is not None and lo <= vpoc_p <= hi)
                out["vpoc_prior"] = {"value": vpoc_p, "naked": naked}
        # VolX — cumulative-volume multiple vs the prior sessions at the matched time-of-day.
        # cc#427 fix_7 / cc#445 fix_5 / cc#454: the anchor session must be RELIABLE. Off-market the latest
        # session can be corrupt/feed-frozen — Fri 10-Jul TRENT had 75 bars present but a full-day volume
        # ~3% of a normal day (mixed cumulative/incremental prints), so VolX read 0.03×/-97%. Skip any
        # anchor whose full-day volume is an outlier-low vs the trailing median (< 30%) and step back to the
        # most recent session with normal-magnitude volume; stamp which session VolX is anchored to. Live ->
        # match to the current bar; off-market -> full-session cum vs prior full-session cums.
        import datetime as _dt
        _dayvol = [(dd, _bars(dd)) for dd in days]
        _dayvol = [(dd, bb, sum((_f(x[3]) or 0) for x in bb)) for dd, bb in _dayvol]
        _vols = sorted(v for _, _, v in _dayvol if v > 0)
        _med = _vols[len(_vols) // 2] if _vols else 0
        vx_idx = None
        for i, (dd, bb, v) in enumerate(_dayvol):
            if v > 0 and (_med <= 0 or v >= 0.30 * _med):
                vx_idx = i
                break
        if vx_idx is not None:
            anchor_day, vx_bars, _ = _dayvol[vx_idx]
            _ist_today = (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()
            _is_live = (anchor_day == _ist_today)
            last_t = vx_bars[-1][4].time() if (_is_live and vx_bars) else _dt.time(23, 59, 59)
            today_cum = sum((_f(x[3]) or 0) for x in vx_bars)
            prior_cums = []
            for (_dd, db, _v) in _dayvol[vx_idx + 1: vx_idx + 6]:
                cum = sum((_f(x[3]) or 0) for x in db if x[4].time() <= last_t)
                if cum > 0:
                    prior_cums.append(cum)
            if prior_cums and today_cum > 0:
                avg = sum(prior_cums) / len(prior_cums)
                vx = round(today_cum / avg, 2) if avg else None
                out["volx"] = vx
                out["volx_pct"] = round((vx - 1.0) * 100.0, 0) if vx is not None else None
                out["volx_asof"] = str(anchor_day)   # cc#454: the session VolX is anchored to
        # hourly % — last 12 5-min closes (1h) change
        if len(tb) >= 2:
            window = tb[-12:] if len(tb) >= 12 else tb
            first_c, last_c = _f(window[0][0]), _f(window[-1][0])
            if first_c:
                out["hourly_pct"] = round((last_c - first_c) / first_c * 100.0, 2)
    return out


# ── cc#445 cockpit v3 helpers ────────────────────────────────────────────────────
def _quadrant_tag(oi_chg, px_chg):
    """OI/price quadrant classification (bullish pair green, bearish red)."""
    if oi_chg is None or px_chg is None:
        return None
    up_oi, up_px = oi_chg > 0, px_chg > 0
    if up_oi and up_px:
        return {"label": "Long Buildup", "color": "bull"}
    if up_oi and not up_px:
        return {"label": "Short Buildup", "color": "bear"}
    if not up_oi and up_px:
        return {"label": "Short Covering", "color": "bull"}
    return {"label": "Long Unwinding", "color": "bear"}


def _results_block(cur, sym) -> Dict[str, Any]:
    """cc#515: Results chip from earnings_calendar (cc#490 forward-date scraper). Never absent --
    a calendar gap (no row at all) is itself information, distinct from a genuinely-fed "no results
    within 3d" gate. status: 'upcoming' (next_date/days_to set), 'recent' (recent_date set, within
    the last 10 days), or 'none' (no row either direction)."""
    cur.execute("""SELECT ex_date FROM earnings_calendar
                   WHERE UPPER(ticker)=UPPER(%s) AND ex_date >= CURRENT_DATE
                   ORDER BY ex_date ASC LIMIT 1""", (sym,))
    nxt = cur.fetchone()
    if nxt and nxt[0]:
        d = nxt[0]
        days_to = (d - date.today()).days
        return {"next_date": str(d), "days_to": days_to, "recent_date": None, "status": "upcoming"}
    cur.execute("""SELECT ex_date FROM earnings_calendar
                   WHERE UPPER(ticker)=UPPER(%s) AND ex_date < CURRENT_DATE
                     AND ex_date >= CURRENT_DATE - INTERVAL '10 days'
                   ORDER BY ex_date DESC LIMIT 1""", (sym,))
    rec = cur.fetchone()
    if rec and rec[0]:
        return {"next_date": None, "days_to": None, "recent_date": str(rec[0]), "status": "recent"}
    return {"next_date": None, "days_to": None, "recent_date": None, "status": "none"}


def _fmt_pct(v):
    return "n/a" if v is None else f"{v:+.2f}"


_QUADRANT_PHRASE = {
    "LONG BUILDUP": "long buildup", "SHORT COVERING": "short covering — price recovering",
    "SHORT BUILDUP": "short buildup — price pressured", "LONG UNWIND": "long unwinding",
}


def _composite_read(quad, results, basis_block, m, opt, ad, intr, recent3d=None, delivery=None) -> Dict[str, Any]:
    """cc#515: ONE deterministic, plain-language desk read synthesizing the tiles the founder
    currently combines mentally -- no LLM, every number quoted comes from the same payload fields
    the tiles themselves render (zero recompute, zero drift). Votes collected: OI quadrant,
    intraday basis direction, ATM put-vs-call OI skew (when fed), A/D 21d label, VolX>=1.3 and
    recent3d/21d volume participation>=1.3 (both energy CONFIRMING the day's price direction, not
    directional alone -- cc#516). DATA THIN votes are excluded from the denominator and
    listed under "dark". A PRICE/FLOW CONFLICT (price direction disagrees with the vote majority)
    forces the label to MIXED regardless of the raw vote count -- the single highest-value read for
    the user, per the founder's MOTHERSON worked example (18-Jul-2026)."""
    votes: List[tuple] = []   # (direction "bull"/"bear", phrase)
    cautions: List[str] = []
    dark: List[str] = []

    # 1) OI quadrant (futures OI x price)
    if quad:
        qdir = "bull" if quad["label"] in ("LONG BUILDUP", "SHORT COVERING") else "bear"
        votes.append((qdir, _QUADRANT_PHRASE.get(quad["label"], quad["label"].lower())))
    else:
        fut_oi = (basis_block or {}).get("fut_oi") or {}
        stale_since = fut_oi.get("stale_since")
        dark.append(f"fut OI (stale since {stale_since})" if (fut_oi.get("stale") and stale_since) else "fut OI")

    # 2) basis direction -- today's intraday open-tick vs latest-tick trend
    bb = (basis_block or {}).get("basis") or {}
    bopen, bnow = bb.get("intraday_open_pct"), bb.get("pct")
    if bopen is not None and bnow is not None:
        delta = bnow - bopen
        if bnow >= 0 and delta > 0:
            votes.append(("bull", f"basis premium widening ({_fmt_pct(bopen)}->{_fmt_pct(bnow)}%)"))
        elif bnow < 0 and delta < 0:
            votes.append(("bear", f"basis discount widening ({_fmt_pct(bopen)}->{_fmt_pct(bnow)}%)"))
        elif bnow >= 0 and delta < 0:
            votes.append(("bear", f"basis premium fading ({_fmt_pct(bopen)}->{_fmt_pct(bnow)}%)"))
        else:
            votes.append(("bull", f"basis discount fading ({_fmt_pct(bopen)}->{_fmt_pct(bnow)}%)"))
    else:
        dark.append("basis")

    # 3) ATM put-vs-call OI d/d skew (when fed -- single-stock option chains are frequently unfed)
    ac, ap = (opt or {}).get("atm_call_oi") or {}, (opt or {}).get("atm_put_oi") or {}
    if ac.get("oi_unfed") or (ac.get("chg_pct") is None and ap.get("chg_pct") is None):
        dark.append("option OI n/f")
    else:
        cc_, pc_ = ac.get("chg_pct"), ap.get("chg_pct")
        if cc_ is not None and pc_ is not None and cc_ != pc_:
            if cc_ > pc_:
                votes.append(("bull", f"call OI building faster than puts ({_fmt_pct(cc_)}% vs {_fmt_pct(pc_)}%)"))
            else:
                votes.append(("bear", f"put OI building faster than calls ({_fmt_pct(pc_)}% vs {_fmt_pct(cc_)}%)"))

    # 4) A/D 21d -- Accumulation/Distribution vote; Neutral is a caution, not a vote
    if ad:
        if ad["label"] == "Accumulation":
            votes.append(("bull", f"accumulation (A/D {ad['up_vol_pct']}%)"))
        elif ad["label"] == "Distribution":
            votes.append(("bear", f"distribution-leaning — dip not being bought (A/D {ad['up_vol_pct']}%)"))
        else:
            cautions.append(f"A/D {ad['up_vol_pct']}% (no accumulation)")

    # 5) VolX -- energy CONFIRMING the day's own price direction (not directional on its own)
    volx, price_chg = (intr or {}).get("volx"), m.get("price_chg")
    if volx is not None and price_chg is not None:
        if volx >= 1.3:
            votes.append(("bull" if price_chg > 0 else "bear", f"VolX {volx}x — energy confirming"))
        elif volx < 0.8:
            cautions.append(f"VolX {volx}x — quiet, move not energy-backed")

    # 6) cc#516: recent3d/21d volume participation -- rising participation confirms the day's price
    # direction (same "confirms, doesn't set" pattern as VolX); drying-up participation is a caution.
    if recent3d is not None and price_chg is not None:
        if recent3d >= 1.3:
            votes.append(("bull" if price_chg > 0 else "bear", f"participation rising ({recent3d}x)"))
        elif recent3d <= 0.8:
            cautions.append(f"participation drying up ({recent3d}x)")

    # 7) cc#517 Part A: delivery-confirmed direction -- conviction volume (ratio>=1.2 vs the
    # symbol's own 21d avg) confirms the day's price direction; churn is a caution.
    if delivery and delivery.get("ratio") is not None and price_chg is not None:
        if delivery["label"] == "conviction":
            votes.append(("bull" if price_chg > 0 else "bear",
                           f"delivery conviction ({delivery['deliv_pct']}% vs {delivery['avg21']}% avg)"))
        elif delivery["label"] == "churn":
            cautions.append(f"delivery churn ({delivery['deliv_pct']}% vs {delivery['avg21']}% avg)")

    n_bull = sum(1 for v in votes if v[0] == "bull")
    n_bear = sum(1 for v in votes if v[0] == "bear")
    m_total = len(votes)
    if n_bull > n_bear:
        direction, n = "BULLISH", n_bull
    elif n_bear > n_bull:
        direction, n = "BEARISH", n_bear
    else:
        direction, n = "MIXED", max(n_bull, n_bear)

    conflict = False
    if price_chg is not None and m_total > 0 and n_bull != n_bear:
        price_dir = "bull" if price_chg > 0 else ("bear" if price_chg < 0 else None)
        vote_dir = "bull" if n_bull > n_bear else "bear"
        if price_dir and price_dir != vote_dir:
            conflict = True
            direction = "MIXED"

    parts = [f"{n}/{m_total} constructive: " + "; ".join(v[1] for v in votes)] if votes else ["no directional votes with data"]
    if cautions:
        parts.append("caution: " + "; ".join(cautions))
    if dark:
        parts.append("dark: " + ", ".join(dark))
    sentence = "; ".join(parts) + "."

    return {"label": f"{direction} FLOW {n}/{m_total}" if m_total else "NO READ 0/0",
            "direction": direction, "n": n, "m": m_total, "conflict": conflict,
            "sentence": sentence, "cautions": cautions, "dark": dark}


def _eod_fut_oi_fallback(cur, sym) -> Optional[Dict[str, Any]]:
    """cc#516 Part C.1b/C.2: read fut_oi_eod (cc#517's nightly NSE bhavcopy job) when the live feed's
    OI is stale/missing. Table-existence-checked first (via information_schema, which never aborts
    the transaction even when the table is absent) so this is forward-compatible -- it activates
    automatically once cc#517 ships and its first nightly run lands a row, no further cc#516 change
    needed. Returns None before that (today: proxy quadrant / stale badge is the honest fallback)."""
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='fut_oi_eod'")
    if not cur.fetchone():
        return None
    cur.execute("SELECT oi, oi_chg_pct, d FROM fut_oi_eod WHERE symbol=%s ORDER BY d DESC LIMIT 1", (sym,))
    r = cur.fetchone()
    if not r:
        return None
    return {"oi": _f(r[0]), "chg_pct": _f(r[1]), "d": str(r[2])}


def _proxy_quadrant(basis_block, price_chg) -> Optional[Dict[str, Any]]:
    """cc#516 Part C.3: when both live and EOD futures OI are dark, derive a LABELED PROXY from
    basis direction + price direction ("price up + premium widening ~ long-buildup-like"). Always
    tagged proxy=True -- never presented as the real OI quadrant (a different color/style + a
    'proxy' suffix in the UI keeps it visually distinct)."""
    bb = (basis_block or {}).get("basis") or {}
    bopen, bnow = bb.get("intraday_open_pct"), bb.get("pct")
    if bopen is None or bnow is None or price_chg is None:
        return None
    delta = bnow - bopen
    price_up = price_chg > 0
    premium_widening = bnow >= 0 and delta > 0
    premium_fading = bnow >= 0 and delta < 0
    discount_widening = bnow < 0 and delta < 0
    if price_up and premium_widening:
        label, color = "Long-buildup-like", "bull"
    elif (not price_up) and premium_widening:
        label, color = "Short-buildup-like", "bear"
    elif (not price_up) and discount_widening:
        label, color = "Long-unwind-like", "bear"
    elif price_up and (premium_fading or (bnow < 0 and delta > 0)):
        label, color = "Short-covering-like", "bull"
    else:
        return None
    return {"label": label, "color": color, "proxy": True,
            "basis_from": bopen, "basis_to": bnow, "price_chg_pct": price_chg}


def _fmt2(v):
    return "--" if v is None else f"{v:.2f}"


def _build_meanings(quad, basis_block, m, opt, ad, intr, atr_d, results, recent3d, delivery=None) -> Dict[str, Optional[str]]:
    """cc#516 Part B: ONE deterministic "meaning" line per tile, template-driven from the SAME
    payload values the tiles themselves render -- no recompute, no LLM. Returned as a flat dict;
    the frontend renders each value (when non-None) as small dim text under its tile."""
    out: Dict[str, Optional[str]] = {}

    vw = (intr or {}).get("vwap") or {}
    if vw.get("dist_pct") is not None:
        out["vwap"] = ("holding above average price" if vw["dist_pct"] >= 0 else "soft finish")

    vt = (intr or {}).get("vpoc_today") or {}
    if vt.get("dist_pct") is not None and vt.get("value") is not None:
        out["vpoc_today"] = (f"overhead supply near {_fmt2(vt['value'])}" if vt["dist_pct"] < 0
                              else f"volume support below at {_fmt2(vt['value'])}")
    vp = (intr or {}).get("vpoc_prior")
    if vp and vp.get("value") is not None:
        out["vpoc_prior"] = (f"naked — magnet at {_fmt2(vp['value'])}" if vp.get("naked")
                              else "tested, support acknowledged")

    fall = (intr or {}).get("fall_from_day_high")
    day_hi = (intr or {}).get("day_hi")
    if fall is not None and fall <= -0.8 and day_hi is not None:
        out["fall_from_day_high"] = f"rallied to {_fmt2(day_hi)}, faded {fall:.2f}% into close"

    volx = (intr or {}).get("volx")
    if volx is not None:
        if volx < 0.8:
            out["volx"] = "quiet — move not energy-backed"
        elif volx >= 1.3:
            out["volx"] = "energy confirming"

    if ad:
        if ad["label"] == "Accumulation":
            out["ad_21d"] = "accumulation"
        elif ad["label"] == "Distribution":
            out["ad_21d"] = "distribution-leaning — dip not being bought"

    bb = (basis_block or {}).get("basis") or {}
    bopen, bnow = bb.get("intraday_open_pct"), bb.get("pct")
    if bopen is not None and bnow is not None:
        delta = bnow - bopen
        if bnow >= 0 and delta > 0:
            out["basis"] = "futures buyers paying up (premium widening)"
        elif bnow >= 0 and delta < 0:
            out["basis"] = "premium fading"
        elif bnow < 0 and delta < 0:
            out["basis"] = "discount widening — futures sellers pressing"
        elif bnow < 0 and delta > 0:
            out["basis"] = "discount fading — sellers losing conviction"

    cost = (opt or {}).get("options_cost")
    if cost:
        atm_expiry = ((opt or {}).get("atm") or {}).get("expiry")
        result_within_expiry = (results and results.get("status") == "upcoming"
                                 and atm_expiry and results.get("next_date")
                                 and results["next_date"] <= atm_expiry)
        if cost.get("label") == "EXPENSIVE":
            if result_within_expiry:
                out["options_cost"] = f"event premium for results {results['next_date']}"
            else:
                out["options_cost"] = "event premium priced with no known result date — confirm results before trading"
        elif cost.get("label") == "CHEAP" and (intr or {}).get("volx") is not None and intr["volx"] >= 1.3:
            out["options_cost"] = "cheap options into rising energy"

    tr_today = (intr or {}).get("tr_today")
    if tr_today is not None and atr_d:
        if tr_today < 0.6 * atr_d:
            out["atr"] = f"compressed range (TR {_fmt2(tr_today)} vs ATR {_fmt2(atr_d)}) — coiling"
        elif tr_today >= 1.3 * atr_d:
            out["atr"] = f"ignition (TR {_fmt2(tr_today)} vs ATR {_fmt2(atr_d)})"

    if recent3d is not None:
        if recent3d >= 1.3:
            out["recent3d_vol_ratio"] = "participation rising"
        elif recent3d <= 0.8:
            out["recent3d_vol_ratio"] = "participation drying up"
        elif recent3d < 1.0:
            out["recent3d_vol_ratio"] = "slightly below baseline"

    if quad and quad.get("proxy"):
        out["oi_quadrant"] = "proxy read — OI dark, inferred from basis + price"

    # cc#517 Part A: delivery meaning + the founder-specified 2x2 with recent3d participation
    # (the decisive corners: quiet+high delivery / loud+low / loud+high / quiet+low).
    if delivery and delivery.get("ratio") is not None:
        if delivery["label"] == "conviction":
            out["delivery"] = "conviction volume — positions carried home"
        elif delivery["label"] == "churn":
            out["delivery"] = "churn"
        else:
            out["delivery"] = f"avg building ({delivery.get('avg21_n', 0)}/21)" if delivery.get("avg21_n", 21) < 21 else None
        if recent3d is not None:
            loud, quiet = recent3d >= 1.3, recent3d <= 0.8
            high, low = delivery["ratio"] >= 1.2, delivery["ratio"] <= 0.7
            if quiet and high:
                out["delivery_combo"] = "quiet accumulation"
            elif loud and low:
                out["delivery_combo"] = "speculative churn"
            elif loud and high:
                out["delivery_combo"] = "institutional-grade move"
    elif delivery and delivery.get("avg21_n") is not None and delivery.get("avg21_n", 0) < 21:
        out["delivery"] = f"avg building ({delivery['avg21_n']}/21)"

    return out


def _basis_tag(pct, is_premium):
    """cc#445 fix_2 / cc#624 item_2: 5d-percentile basis tag as (label, color). PREMIUM keeps
    STRONG (>=70, green) / WEAK (<=30, red) / AVERAGE (between, amber). DISCOUNT relabels to
    DEEP (<=30, red — near the bottom of its 5d range) / FADING (>=70, green — discount shrinking
    toward premium) / AVERAGE (amber). Founder flagged the old 'DISCOUNT · STRONG' as misleading."""
    if pct is None:
        return None, None
    if is_premium:
        if pct >= 70:
            return "STRONG", "grn"
        if pct <= 30:
            return "WEAK", "red"
        return "AVERAGE", "amb"
    if pct <= 30:
        return "DEEP", "red"
    if pct >= 70:
        return "FADING", "grn"
    return "AVERAGE", "amb"


def _levels_verdict(intr, delivery, price_chg, cmp_px):
    """cc#624 item_4: LEVELS structural verdict — STRONG (green) / MODERATE (amber) / WEAK (red) from
    5 deadbanded structural votes (deadbands kill noise-flips). Assembled from ALREADY-computed level
    values so the chip can never drift from the tiles. ATR coiling/ignition is a caution/context line,
    NEVER a vote (non-directional). Verdict: denominator = non-neutral votes; bull majority = STRONG,
    bear majority = WEAK, tie or 0 = MODERATE. Display 'STRONG 3/4' = winning-side count / non-neutral."""
    intr = intr or {}
    votes = []
    # V1 VWAP distance: >= +0.3% bull, <= -0.3% bear, else neutral
    vw = (intr.get("vwap") or {}).get("dist_pct")
    if vw is not None:
        votes.append("bull" if vw >= 0.3 else "bear" if vw <= -0.3 else "neutral")
    # V2 VPOC-today: CMP above by >0.3% = bull (volume support below), below by >0.3% = bear
    #   (dist_pct is (cmp - vpoc)/vpoc*100)
    vt = (intr.get("vpoc_today") or {}).get("dist_pct")
    if vt is not None:
        votes.append("bull" if vt > 0.3 else "bear" if vt < -0.3 else "neutral")
    # V3 Prior-VPOC MAGNET (corrected direction): NAKED above CMP = bull (upward revisit pull),
    #   NAKED below = bear (downward pull), tested = neutral
    vp = intr.get("vpoc_prior") or {}
    if vp.get("value") is not None and cmp_px:
        if not vp.get("naked"):
            votes.append("neutral")
        elif vp["value"] > cmp_px:
            votes.append("bull")
        elif vp["value"] < cmp_px:
            votes.append("bear")
        else:
            votes.append("neutral")
    # V4 ORB position: Above = bull, Below = bear, Inside = neutral
    orbpos = (intr.get("orb") or {}).get("position")
    if orbpos:
        votes.append("bull" if orbpos == "Above" else "bear" if orbpos == "Below" else "neutral")
    # V5 Delivery CONFIRMS day direction: ratio >= 1.2 votes WITH the day price sign; churn (<=0.7)
    #   is a caution line, not a vote (composite-READ doctrine)
    caution = None
    if delivery and delivery.get("ratio") is not None and price_chg is not None:
        r = delivery["ratio"]
        if r >= 1.2 and price_chg != 0:
            votes.append("bull" if price_chg > 0 else "bear")
        elif r <= 0.7:
            caution = f"delivery churn ({delivery.get('deliv_pct')}%) — low conviction"
    n_bull, n_bear = votes.count("bull"), votes.count("bear")
    nn = n_bull + n_bear
    if nn == 0 or n_bull == n_bear:
        label, color = "MODERATE", "amb"
    elif n_bull > n_bear:
        label, color = "STRONG", "grn"
    else:
        label, color = "WEAK", "red"
    return {"label": label, "color": color, "score": f"{max(n_bull, n_bear)}/{nn}",
            "n_bull": n_bull, "n_bear": n_bear, "non_neutral": nn, "caution": caution}


def _ad_21d(cur, sym):
    """cc#445 fix_5: 21-day Accumulation/Distribution — up-day volume vs down-day volume balance."""
    cur.execute("""SELECT close, volume FROM raw_prices WHERE symbol=%s AND close IS NOT NULL
                   ORDER BY price_date DESC LIMIT 22""", (sym,))
    rows = [(_f(r[0]), _f(r[1]) or 0.0) for r in cur.fetchall()][::-1]
    if len(rows) < 3:
        return None
    up_vol = dn_vol = 0.0
    for i in range(1, len(rows)):
        if rows[i][0] is None or rows[i - 1][0] is None:
            continue
        if rows[i][0] > rows[i - 1][0]:
            up_vol += rows[i][1]
        elif rows[i][0] < rows[i - 1][0]:
            dn_vol += rows[i][1]
    tot = up_vol + dn_vol
    if tot <= 0:
        return None
    up_pct = round(up_vol / tot * 100.0, 0)
    label = "Accumulation" if up_pct >= 55 else "Distribution" if up_pct <= 45 else "Neutral"
    return {"up_vol_pct": up_pct, "label": label, "days": len(rows) - 1}


def _recent3d_vol_ratio(cur, sym) -> Optional[float]:
    """cc#516 Part D (founder-specified): AVG(volume, t-1..t-3) / AVG(volume, t-1..t-21) -- the base
    average INCLUDES the recent 3 sessions, per the founder's exact formula. >=1.3 = participation
    rising, 0.8-1.3 = neutral, <=0.8 = participation drying up (ENERGY tile "Recent participation")."""
    cur.execute("""SELECT volume FROM raw_prices WHERE symbol=%s AND volume IS NOT NULL
                   ORDER BY price_date DESC LIMIT 21""", (sym,))
    vols = [_f(r[0]) for r in cur.fetchall()]
    if len(vols) < 21:
        return None
    avg3 = sum(vols[:3]) / 3.0
    avg21 = sum(vols[:21]) / 21.0
    return round(avg3 / avg21, 2) if avg21 else None


def _delivery_block(cur, sym) -> Optional[Dict[str, Any]]:
    """cc#517 Part A: latest delivery% vs the symbol's OWN 21d average (never an absolute band).
    Reads delivery_eod (cc#517's nightly NSE ingest) -- returns None gracefully before that table
    has rows for this symbol (pre-first-run, or a non-EQ/non-F&O name)."""
    try:
        from nse_eod_ingest import delivery_21d_avg
    except Exception:
        return None
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='delivery_eod'")
    if not cur.fetchone():
        return None
    cur.execute("SELECT d, deliv_pct, traded_qty FROM delivery_eod WHERE symbol=%s ORDER BY d DESC LIMIT 1", (sym,))
    r = cur.fetchone()
    if not r or r[1] is None:
        return None
    d_latest, deliv_pct, traded_qty = r[0], float(r[1]), r[2]
    avg21, n = delivery_21d_avg(cur, sym, d_latest)
    ratio = round(deliv_pct / avg21, 2) if avg21 else None
    label = None
    if ratio is not None:
        label = "conviction" if ratio >= 1.2 else ("churn" if ratio <= 0.7 else None)
    ser = delivery_series(cur, sym, 30)   # cc#589: 30-trading-day deliv% trend for the V-panel sparkline
    return {"d": str(d_latest), "deliv_pct": deliv_pct, "avg21": avg21, "avg21_n": n,
            "ratio": ratio, "label": label, "traded_qty": traded_qty,
            "series": ser["series"], "avg30": ser["avg"], "avg30_n": ser["n"]}


def delivery_series(cur, sym, days=30):
    """cc#589: last `days` trading days of deliv_pct for `sym`, oldest->newest, for the V-panel
    trend sparkline. Degrades gracefully — returns whatever exists (delivery_eod began 20-Jul-2026,
    so the 30d window fills progressively). avg = simple mean of the returned non-null points."""
    try:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='delivery_eod'")
        if not cur.fetchone():
            return {"series": [], "latest": None, "avg": None, "n": 0, "days": days}
        cur.execute("""SELECT d, deliv_pct FROM delivery_eod
                       WHERE symbol=%s AND deliv_pct IS NOT NULL
                       ORDER BY d DESC LIMIT %s""", (sym, days))
        rows = cur.fetchall()[::-1]   # oldest -> newest
        series = [{"d": str(r[0]), "deliv_pct": round(float(r[1]), 1)} for r in rows]
        vals = [p["deliv_pct"] for p in series]
        avg = round(sum(vals) / len(vals), 1) if vals else None
        return {"series": series, "latest": (vals[-1] if vals else None),
                "avg": avg, "n": len(series), "days": days}
    except Exception:
        return {"series": [], "latest": None, "avg": None, "n": 0, "days": days}


def _fo_banned_today(cur, sym) -> bool:
    """cc#517 Part D: SETUP CONTEXT chip only (display) -- the real entry-skip gate lives in
    v8_signal_writer.py. Table-exists-checked first so this is a no-op before fo_ban's first row."""
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='fo_ban'")
    if not cur.fetchone():
        return False
    cur.execute("SELECT 1 FROM fo_ban WHERE d=(SELECT MAX(d) FROM fo_ban) AND symbol=%s", (sym,))
    return cur.fetchone() is not None


def _atr_daily(cur, sym, period=14):
    """cc#445 fix_6: daily ATR(14) from raw_prices."""
    cur.execute("""SELECT high, low, close FROM raw_prices WHERE symbol=%s AND close IS NOT NULL
                   ORDER BY price_date DESC LIMIT %s""", (sym, period + 1))
    rows = [(_f(r[0]), _f(r[1]), _f(r[2])) for r in cur.fetchall()][::-1]
    if len(rows) < 2:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, c = rows[i]; pc = rows[i - 1][2]
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs) / len(trs), 2) if trs else None


def _ensure_oi_daily(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS options_oi_daily (
        underlying TEXT NOT NULL, d DATE NOT NULL, atm_strike NUMERIC,
        call_oi BIGINT, put_oi BIGINT, snapshot_ts TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (underlying, d))""")


def persist_atm_oi_daily(cur, sym, atm, call_oi, put_oi):
    """cc#445 fix_4: upsert today's ATM call/put OI into options_oi_daily (post-open + EOD job) so a
    real day-over-day is available from the 2nd session onward."""
    if atm is None or (call_oi is None and put_oi is None):
        return
    from datetime import date as _date
    _ensure_oi_daily(cur)
    cur.execute("""INSERT INTO options_oi_daily (underlying, d, atm_strike, call_oi, put_oi)
                   VALUES (%s, CURRENT_DATE, %s, %s, %s)
                   ON CONFLICT (underlying, d) DO UPDATE SET atm_strike=EXCLUDED.atm_strike,
                       call_oi=EXCLUDED.call_oi, put_oi=EXCLUDED.put_oi, snapshot_ts=NOW()""",
                (sym, atm, call_oi, put_oi))


def _atm_dd_from_daily(cur, sym, atm, call_oi, put_oi):
    """cc#445 fix_4: ATM Call/Put OI d/d from options_oi_daily (today vs prev trading-day snapshot).
    Returns (call_chg, put_chg, first_snapshot, as_of). '1st snapshot' until 2 sessions exist."""
    _ensure_oi_daily(cur)
    cur.execute("""SELECT d, call_oi, put_oi FROM options_oi_daily WHERE underlying=%s
                   ORDER BY d DESC LIMIT 2""", (sym,))
    rows = cur.fetchall()
    if not rows:
        return None, None, True, None
    latest_d = rows[0][0]
    if len(rows) < 2:
        return None, None, True, str(latest_d)
    prev_c, prev_p = _f(rows[1][1]), _f(rows[1][2])
    cc = round((call_oi - prev_c) / prev_c * 100.0, 1) if (call_oi is not None and prev_c) else None
    pc = round((put_oi - prev_p) / prev_p * 100.0, 1) if (put_oi is not None and prev_p) else None
    return cc, pc, False, str(latest_d)


def snapshot_all_atm_oi(conn) -> Dict[str, Any]:
    """cc#445 fix_4: persist today's ATM call/put OI per F&O underlying into options_oi_daily so the
    cockpit ATM Call/Put OI d/d has a real prior-day snapshot. Called post-open + EOD (trading days)."""
    with conn.cursor() as cur:
        _ensure_oi_daily(cur)
        cur.execute("SELECT DISTINCT underlying FROM option_chain WHERE ts >= NOW() - INTERVAL '3 days'")
        unders = [r[0] for r in cur.fetchall()]
    n = 0
    for u in unders:
        try:
            with conn.cursor() as cur:
                b = _basis_block(cur, u)
                cmp_px = b.get("fut") or b.get("spot")
                if cmp_px is None:
                    continue
                opt = _options_block(cur, u, cmp_px)
                atm = (opt.get("atm_call_oi") or {}).get("strike")
                coi = (opt.get("atm_call_oi") or {}).get("oi")
                poi = (opt.get("atm_put_oi") or {}).get("oi")
                if atm is not None and (coi is not None or poi is not None):
                    persist_atm_oi_daily(cur, u, atm, coi, poi)
                    conn.commit(); n += 1
        except Exception as e:
            log.warning(f"snapshot_atm {u}: {e}")
    return {"snapshotted": n, "underlyings": len(unders)}


def check_oi_feed_degradation(conn) -> Dict[str, Any]:
    """cc#515: universe-wide OI-staleness diagnostic. Per-symbol staleness is already surfaced
    honestly in the cockpit (fut_oi.stale/stale_since above); this catches the FEED-WIDE case (like
    the 16-Jul incident) where a large slice of the universe goes stale together -- that reads as
    "every cockpit quietly degraded" one symbol at a time unless something raises a single alert.
    "> 2 sessions old" = the latest non-null OI predates the 3rd-most-recent session with any
    futures_basis rows. Pure diagnostic -- returns stats only; the caller (scheduler.py's
    _bg_oi_feed_health, via _log_alert) decides whether/how to alert and at what cadence."""
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active=TRUE")
        universe = [r[0] for r in cur.fetchall()]
        if not universe:
            return {"checked": 0, "stale": 0, "pct": 0.0}
        cur.execute("""SELECT DISTINCT ts::date FROM futures_basis
                       ORDER BY ts::date DESC LIMIT 3""")
        sessions = [r[0] for r in cur.fetchall()]
        cutoff = sessions[2] if len(sessions) >= 3 else (sessions[-1] if sessions else None)
        stale = 0
        for sym in universe:
            cur.execute("SELECT MAX(ts::date) FROM futures_basis WHERE symbol=%s AND oi IS NOT NULL", (sym,))
            r = cur.fetchone()
            last = r[0] if r else None
            if last is None or (cutoff is not None and last < cutoff):
                stale += 1
        pct = round(stale / len(universe) * 100.0, 1)
        return {"checked": len(universe), "stale": stale, "pct": pct,
                "cutoff_session": str(cutoff) if cutoff else None}


@deriv_router.get("/api/delivery/series")
def delivery_series_endpoint(symbol: str, days: int = 30):
    """cc#589: deliv_pct trend (last `days` trading days) for the V/view-detail panel on the V8 +
    GVM screens. Graceful partial history — returns whatever delivery_eod holds (fills progressively)."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, "symbol required")
    days = max(1, min(int(days or 30), 120))
    try:
        with _conn() as conn, conn.cursor() as cur:
            out = delivery_series(cur, sym, days)
        out["symbol"] = sym
        return out
    except Exception as e:
        return {"symbol": sym, "series": [], "latest": None, "avg": None, "n": 0, "days": days,
                "error": str(e)}


@deriv_router.get("/api/deriv-metrics/{symbol}")
def deriv_metrics(symbol: str, side: Optional[str] = None):
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, "symbol required")
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_iv_table(cur)
            basis = _basis_block(cur, sym)
            fut = basis.get("fut")
            spot = basis.get("spot")
            cmp_px = fut or spot   # futures-first, consistent with the position CMP
            m = _safe("metrics", lambda: _metrics(cur, sym), {})
            oi_chg = (basis.get("fut_oi") or {}).get("chg_pct")
            opt = _safe("options", lambda: _options_block(cur, sym, cmp_px), {"has_options": False})
            intr = _safe("intraday", lambda: _intraday_block(cur, sym, cmp_px), {})
            oi_5d = _safe("oi_5d", lambda: _oi_5d_rolling(cur, sym))   # cc#427 fix_8
            # cc#445 fix_3: futures-OI quadrant tag (OI d/d × price change)
            _fq = _safe("fut_quadrant", lambda: _quadrant_tag(oi_chg, m.get("price_chg")))
            if basis.get("fut_oi"):
                basis["fut_oi"]["quadrant"] = _fq["label"] if _fq else None
                basis["fut_oi"]["quadrant_color"] = _fq["color"] if _fq else None
                basis["fut_oi"]["price_chg"] = m.get("price_chg")
            # cc#515: Results chip (earnings_calendar) — never absent; a calendar gap is itself info.
            results = _safe("results", lambda: _results_block(cur, sym),
                             {"next_date": None, "days_to": None, "recent_date": None, "status": "none"})
            # cc#449/cc#446 fix_1: ATM Call/Put OI d/d — the option_chain latest-2-days d/d computed in
            # _options_block (atm_call_oi/atm_put_oi.chg_pct + first_snapshot) is the PRIMARY source
            # (chain has 4-5 snapshot days per underlying). The options_oi_daily store is only a FALLBACK
            # when the chain carries a single snapshot day, so a symbol with deeper daily history still
            # shows a real d/d instead of "1st snapshot". [cc#445 fix_4 REGRESSION fixed: it overrode the
            # good chain d/d with the empty daily store AND called _atm_dd_from_daily with 4 of 5 args
            # (TypeError -> whole cockpit 500'd for every symbol).]
            def _atm_dd_fallback():
                ac, ap = opt.get("atm_call_oi"), opt.get("atm_put_oi")
                if not (ac or ap):
                    return
                if not ((ac or {}).get("first_snapshot") or (ap or {}).get("first_snapshot")):
                    return   # chain already has a real d/d — keep it
                _atm = (ac or ap or {}).get("strike")
                _coi = (ac or {}).get("oi")
                _poi = (ap or {}).get("oi")
                _cdd, _pdd, _firstsnap, _dd_asof = _atm_dd_from_daily(cur, sym, _atm, _coi, _poi)
                if _cdd is not None and ac:
                    ac.update({"chg_pct": _cdd, "first_snapshot": False, "dd_asof": _dd_asof})
                if _pdd is not None and ap:
                    ap.update({"chg_pct": _pdd, "first_snapshot": False, "dd_asof": _dd_asof})
            _safe("atm_dd_fallback", _atm_dd_fallback)
            # cc#445 fix_5/fix_6: A/D 21d + daily ATR
            ad = _safe("ad_21d", lambda: _ad_21d(cur, sym))
            atr_d = _safe("atr_daily", lambda: _atr_daily(cur, sym))
            # cc#516 Part D: 3d/21d volume participation ratio
            recent3d = _safe("recent3d_vol_ratio", lambda: _recent3d_vol_ratio(cur, sym))
            # cc#516 Part C.1b/C.2: EOD futures-OI fallback (cc#517's nightly bhavcopy job) --
            # forward-compatible no-op until that table exists / carries a row for this symbol.
            eod_fut_oi = _safe("eod_fut_oi", lambda: _eod_fut_oi_fallback(cur, sym))
            # cc#517 Part A: delivery vs own 21d avg
            delivery = _safe("delivery", lambda: _delivery_block(cur, sym))
            # cc#517 Part D: F&O ban SETUP CONTEXT chip (display-only; the actual entry-skip gate
            # lives in v8_signal_writer.py's _auto_paper_entry)
            fo_banned = _safe("fo_banned", lambda: _fo_banned_today(cur, sym), False)
            # cc#368: freshness stamp = latest available option_chain snapshot for this underlying.
            # The chain blocks already read MAX(ts) (never today-only), so off-market/weekend still
            # returns the last live snapshot; data_ts lets the UI label it honestly ("as of <ts>")
            # and, when NULL, flip to an explicit "No option data" state instead of an infinite skeleton.
            def _chain_ts():
                cur.execute("SELECT MAX(ts) FROM option_chain WHERE underlying=%s", (sym,))
                _cts = cur.fetchone()
                return _cts[0].isoformat() if _cts and _cts[0] else None
            chain_ts = _safe("chain_ts", _chain_ts)

        tc = _safe("tc_score", lambda: _tc_score(sym, side))   # opens its own connection — kept outside the block above

        # cc#515: verdict.oi_quadrant now ALWAYS returns a dict (never bare None) so the tile can
        # show "OI stale since <date>" instead of an unexplained "DATA THIN" when the quadrant is
        # unavailable specifically because the OI feed has gone stale (not just missing today).
        # cc#516 Part C: fallback chain when live is dark -- EOD bhavcopy (cc#517, once it exists) ->
        # labeled proxy (basis+price) -> honest stale/DATA THIN. The proxy is NEVER presented as the
        # real quadrant (proxy=True + a distinct label suffix, enforced client-side too).
        _quad_raw = _safe("oi_quadrant", lambda: _oi_quadrant(oi_chg, m.get("price_chg")))
        _fut_oi = basis.get("fut_oi") or {}
        _quad_eod = None
        if _quad_raw is None and eod_fut_oi and eod_fut_oi.get("chg_pct") is not None:
            _quad_eod = _safe("oi_quadrant_eod", lambda: _quadrant_tag(eod_fut_oi["chg_pct"], m.get("price_chg")))
            if _quad_eod:
                _quad_eod = {"label": _quad_eod["label"].upper(), "color": _quad_eod["color"]}
        _quad_proxy = None
        if _quad_raw is None and _quad_eod is None:
            _quad_proxy = _safe("proxy_quadrant", lambda: _proxy_quadrant(basis, m.get("price_chg")))
        _quad_display = _quad_raw or _quad_eod
        oi_quadrant_resp = {
            "label": _quad_display["label"] if _quad_display else (_quad_proxy["label"] if _quad_proxy else None),
            "color": _quad_display["color"] if _quad_display else (_quad_proxy["color"] if _quad_proxy else None),
            "oi_chg_pct": oi_chg, "price_chg_pct": m.get("price_chg"),
            "stale": bool(_fut_oi.get("stale")), "stale_since": _fut_oi.get("stale_since"),
            "source": "live" if _quad_raw else ("eod" if _quad_eod else ("proxy" if _quad_proxy else None)),
            "proxy": bool(_quad_proxy is not None and _quad_display is None),
        }
        # cc#515/516: composite plain-language READ tile -- one deterministic desk synthesis of the
        # tiles above, cited from these SAME already-computed values (no recompute, no drift).
        read = _safe("composite_read",
                      lambda: _composite_read(_quad_raw, results, basis, m, opt, ad, intr, recent3d, delivery),
                      {"label": "NO READ", "direction": "MIXED", "n": 0, "m": 0, "conflict": False,
                       "sentence": "insufficient data.", "cautions": [], "dark": []})
        # cc#516 Part B: per-tile interpretation lines
        meanings = _safe("meanings",
                          lambda: _build_meanings(oi_quadrant_resp, basis, m, opt, ad, intr, atr_d, results, recent3d, delivery),
                          {})

        resp = {
            "symbol": sym, "cmp": cmp_px, "fut": fut, "spot": spot, "side": (side or "").upper() or None,
            "has_options": opt.get("has_options", False),
            "data_ts": chain_ts,   # cc#368: latest option_chain snapshot ts (None = no chain rows)
            "results": results,   # cc#515: Results chip
            "read": read,         # cc#515: composite READ tile
            "meanings": meanings, # cc#516 Part B: per-tile interpretation lines
            "fo_banned": fo_banned,   # cc#517 Part D: SETUP CONTEXT "F&O BAN" chip (display only)
            "verdict": {
                "oi_quadrant": oi_quadrant_resp,
                "options_cost": opt.get("options_cost"),
                "tc_score": tc,
            },
            "levels": {
                "vpoc_today": intr.get("vpoc_today"),
                "vpoc_prior": intr.get("vpoc_prior"),
                "vwap": intr.get("vwap"),
                "orb": intr.get("orb"),   # cc#445 fix_8
                # cc#624 item_4: structural LEVELS verdict (STRONG/MODERATE/WEAK), assembled from the
                # already-computed level values above so it can never drift from the tiles.
                "verdict": _safe("levels_verdict",
                                 lambda: _levels_verdict(intr, delivery, m.get("price_chg"), cmp_px)),
            },
            "flow": {
                "fut_oi": basis.get("fut_oi"),
                "atm_call_oi": opt.get("atm_call_oi"),
                "atm_put_oi": opt.get("atm_put_oi"),
                "basis": basis.get("basis"),
                "oi_5d": oi_5d,   # cc#427 fix_8: 5-day futures OI rolling (cc#445: dated + quadrant)
            },
            "energy": {"volx": intr.get("volx"), "volx_pct": intr.get("volx_pct"),
                       "volx_asof": intr.get("volx_asof"),   # cc#454: session VolX is anchored to
                       "ad_21d": ad,   # cc#445 fix_5
                       "atr_5m": intr.get("atr_5m"), "atr_5m_pct": intr.get("atr_5m_pct"),   # cc#445 fix_6
                       "atr_daily": atr_d, "tr_today": intr.get("tr_today"),
                       "atr_daily_pct": round(atr_d / cmp_px * 100.0, 2) if (atr_d and cmp_px) else None,
                       "recent3d_vol_ratio": recent3d,     # cc#516 Part D
                       "delivery": delivery},              # cc#517 Part A
            "rsi": {"d": m.get("rsi_d"), "w": m.get("rsi_w")},
        }
        return resp
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"deriv_metrics {sym}: {e}", exc_info=True)
        raise HTTPException(500, f"deriv_metrics failed: {e}")
