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
    return {"fut": fut, "spot": spot,
            "basis": {"value": basis, "pct": basis_pct, "percentile": pct, "spark": spark},
            "fut_oi": {"oi": oi, "chg_pct": oi_dd_pct}}


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
    """cc#427 fix_8: last-5-session futures OI day-over-day % (one OI per session), plus the net-5d
    figure. Feeds the "OI · 5d rolling" mini-row. Needs >=2 sessions or returns None (row hidden)."""
    cur.execute("""SELECT DISTINCT ON (ts::date) ts::date, oi FROM futures_basis
                   WHERE symbol=%s AND oi IS NOT NULL
                   ORDER BY ts::date DESC, ts DESC LIMIT 6""", (sym,))
    rows = [(r[0], _f(r[1])) for r in cur.fetchall()][::-1]   # oldest -> newest
    if len(rows) < 2:
        return None
    series = []
    for i in range(1, len(rows)):
        prev, oi = rows[i - 1][1], rows[i][1]
        pct = round((oi - prev) / prev * 100.0, 1) if (prev and prev > 0) else None
        series.append({"date": str(rows[i][0]), "chg_pct": pct})
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
    return {
        "has_options": True,
        "atm_call_oi": {"strike": atm, "oi": atm_call_oi, "chg_pct": atm_call_chg, "first_snapshot": _first_snap},
        "atm_put_oi": {"strike": atm, "oi": atm_put_oi, "chg_pct": atm_put_chg, "first_snapshot": _first_snap},
        "call_oi": {"oi": call_oi, "chg_pct": call_chg},
        "put_oi": {"oi": put_oi, "chg_pct": put_chg},
        "pcr": {"value": pcr, "chg": pcr_chg},
        "oi_walls": {"call": _wall(call_wall), "put": _wall(put_wall)},
        "atm": {
            "strike": atm,
            "ce_ltp": ce_ltp, "ce_bid": _f(ce[4]) if ce else None, "ce_ask": _f(ce[5]) if ce else None,
            "pe_ltp": pe_ltp, "pe_bid": _f(pe[4]) if pe else None, "pe_ask": _f(pe[5]) if pe else None,
            "gap": gap,
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
        # fall-from-day-high
        if hi and cmp_px:
            out["fall_from_day_high"] = round((cmp_px - hi) / hi * 100.0, 2)
        # prior-day naked VPOC (prior VPOC untouched by today's range)
        if len(days) > 1:
            pb = _bars(days[1])
            vpoc_p = _vpoc(pb)
            if vpoc_p is not None:
                naked = not (lo is not None and hi is not None and lo <= vpoc_p <= hi)
                out["vpoc_prior"] = {"value": vpoc_p, "naked": naked}
        # VolX — today's cum volume to the latest bar-time vs the avg cum volume at the
        # same time-of-day over the prior up-to-5 sessions (time-matched multiple).
        # cc#427 fix_7: if the anchor session carries no volume (a dead/empty latest print off-market),
        # fall back to the last session that DID trade so VolX is never a misleading 0.0×. Also expose
        # volx_pct = the % above/below a typical session (e.g. 1.4× -> +40%).
        vx_bars, vx_days = tb, days
        if sum((_f(x[3]) or 0) for x in tb) <= 0:
            for j in range(1, len(days)):
                cand = _bars(days[j])
                if cand and sum((_f(x[3]) or 0) for x in cand) > 0:
                    vx_bars, vx_days = cand, days[j:]
                    break
        last_t = vx_bars[-1][4].time()
        today_cum = sum((_f(x[3]) or 0) for x in vx_bars)
        prior_cums = []
        for d in vx_days[1:6]:
            db = _bars(d)
            cum = sum((_f(x[3]) or 0) for x in db if x[4].time() <= last_t)
            if cum > 0:
                prior_cums.append(cum)
        if prior_cums and today_cum > 0:
            avg = sum(prior_cums) / len(prior_cums)
            vx = round(today_cum / avg, 2) if avg else None
            out["volx"] = vx
            out["volx_pct"] = round((vx - 1.0) * 100.0, 0) if vx is not None else None
        # hourly % — last 12 5-min closes (1h) change
        if len(tb) >= 2:
            window = tb[-12:] if len(tb) >= 12 else tb
            first_c, last_c = _f(window[0][0]), _f(window[-1][0])
            if first_c:
                out["hourly_pct"] = round((last_c - first_c) / first_c * 100.0, 2)
    return out


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
            m = _metrics(cur, sym)
            oi_chg = (basis.get("fut_oi") or {}).get("chg_pct")
            opt = _options_block(cur, sym, cmp_px)
            intr = _intraday_block(cur, sym, cmp_px)
            oi_5d = _oi_5d_rolling(cur, sym)   # cc#427 fix_8
            # cc#368: freshness stamp = latest available option_chain snapshot for this underlying.
            # The chain blocks already read MAX(ts) (never today-only), so off-market/weekend still
            # returns the last live snapshot; data_ts lets the UI label it honestly ("as of <ts>")
            # and, when NULL, flip to an explicit "No option data" state instead of an infinite skeleton.
            cur.execute("SELECT MAX(ts) FROM option_chain WHERE underlying=%s", (sym,))
            _cts = cur.fetchone()
            chain_ts = _cts[0].isoformat() if _cts and _cts[0] else None

        tc = _tc_score(sym, side)   # opens its own connection — kept outside the block above

        resp = {
            "symbol": sym, "cmp": cmp_px, "fut": fut, "spot": spot, "side": (side or "").upper() or None,
            "has_options": opt.get("has_options", False),
            "data_ts": chain_ts,   # cc#368: latest option_chain snapshot ts (None = no chain rows)
            "verdict": {
                "oi_quadrant": _oi_quadrant(oi_chg, m.get("price_chg")),
                "options_cost": opt.get("options_cost"),
                "tc_score": tc,
            },
            "levels": {
                "vpoc_today": intr.get("vpoc_today"),
                "vpoc_prior": intr.get("vpoc_prior"),
                "vwap": intr.get("vwap"),
            },
            "flow": {
                "fut_oi": basis.get("fut_oi"),
                "atm_call_oi": opt.get("atm_call_oi"),
                "atm_put_oi": opt.get("atm_put_oi"),
                "basis": basis.get("basis"),
                "oi_5d": oi_5d,   # cc#427 fix_8: 5-day futures OI rolling
            },
            "energy": {"volx": intr.get("volx"), "volx_pct": intr.get("volx_pct")},
            "rsi": {"d": m.get("rsi_d"), "w": m.get("rsi_w")},
        }
        return resp
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"deriv_metrics {sym}: {e}", exc_info=True)
        raise HTTPException(500, f"deriv_metrics failed: {e}")
