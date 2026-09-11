"""
option_ivp.py — cc#1859 OPTION VALUE: skew-aware IV percentile fair value from the bhavcopy year.

Reads option_iv_daily (cc#1858, populated nightly by option_iv_history.py off the NSE F&O
bhavcopy) ONLY. Never writes it (do_not_touch on the card). No new tables, no ALTER TABLE.

THE METHOD, exactly as ruled across cc_task_logs on cc#1859/1199 (session_log trail: 6264, 6296,
6297, 6301, 6309, 6310, 6311, 6312):

  LEVEL (the ATM anchor) — RULED 3, log 6301: the ATM fair IV for a session is the AVERAGE of
  that session's ATM CALL IV and ATM PUT IV (same strike-nearest-to-spot, same expiry — every
  (symbol, trade_date) in option_iv_daily carries exactly one expiry, confirmed empirically, so
  "nearest expiry" needs no tie-break). A call and a put at the same strike/expiry have IDENTICAL
  VEGA, so a synthetic-forward error moves the two solved IVs by equal and opposite amounts and
  averaging cancels it to first order — this is why the level uses the blend.

  SHAPE (the skew) — R3, log 6312: every bucket's skew is measured WITHIN ITS OWN LEG against
  that SAME leg's ATM IV (never against the blended ATM) — skew(bucket, leg, session) =
  leg_iv(bucket, session) - leg_iv(ATM, session, SAME leg). This makes the ATM bucket's own skew
  exactly 0.0 on every session by construction — a free sanity check, asserted in the tests below.
  A blended anchor was measured to inject a call/put-asymmetric artefact (every call bucket
  reading ~2.5pts too cheap, every put bucket ~2.5pts too rich, forever) — the exact RV20-shaped
  defect this card exists to avoid, just wearing a new coat. Rejected, not used.

  WINDOW — log 6311/6312: TRAILING 120 TRADING SESSIONS (not calendar days), minimum 60 sessions
  or no tag / no fair value, in plain words why. Governs BOTH statistics below, consistently:
    - MEDIAN of the window → the FAIR IV, fed to Black-Scholes → the FAIR VALUE (a price).
    - PERCENTILE RANK of today's own value within the window → the IVP → the TAG.
  (Fable's own worked NATIONALUM figure, sigma 0.3357, was the FULL-history median, computed
  before the window was ruled — quoted here for the record, not the number this module produces;
  RULED 3 explicitly says not to chase small figure differences further.)

  BANDS — log 6311: CHEAP < 25, FAIR 25-75, EXPENSIVE > 75 (IVP percentile, 0-100).

  BUCKETS — matching reports/CC1859_bucket_coverage.md exactly so this module's numbers are the
  SAME numbers already reported to the founder: moneyness = ROUND((strike/spot - 1) * 100), whole
  percent, split by option_type (CE/PE, each its own leg) and by expiry class (wk = DTE<=10,
  mo = DTE>10). A bucket needs 60+ distinct trade_dates in the window to be usable; short of that,
  NO TAG on that bucket, ever (G1/G2 — never lower the floor to make a tag appear).

  SANITY GATE — G3, cc#1847: IV floor 3% / ceiling 150%, applied when building the HISTORY series
  (a row failing it never enters a median/percentile) — a floored or ceilinged IV is a solver
  failure (deep-ITM priced under intrinsic, or similar), not a real observation, matching the
  cc#1994 build's own floor-artifact finding on this identical table.

DOES NOT price or tag anything beyond ATM+-5 (F5/G2 for stocks: wing buckets with insufficient
depth simply carry no tag — this module never widens the band on its own). Does not touch
deriv_metrics.py's Black-Scholes solver — _bs_price is imported and used as-is (F2, "no second
pricer").
"""
import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("scorr.option_ivp")

WINDOW_SESSIONS = 120        # RULED, log 6311/6312
MIN_SESSIONS = 60            # RULED floor — below this, no tag, no fair value (G1/G2)
IV_FLOOR = 0.03              # G3 / cc#1847 sanity gate
IV_CEILING = 1.50            # G3 / cc#1847 sanity gate
BAND_CHEAP_MAX = 25.0        # RULED bands, log 6311
BAND_EXPENSIVE_MIN = 75.0
BUCKET_MIN_SESSIONS = 60     # G1: a moneyness bucket needs 60+ distinct trade_dates to tag


def _band(ivp: Optional[float]) -> Optional[str]:
    if ivp is None:
        return None
    if ivp < BAND_CHEAP_MAX:
        return "CHEAP"
    if ivp > BAND_EXPENSIVE_MIN:
        return "EXPENSIVE"
    return "FAIR"


def _percentile_rank(value: float, window: List[float]) -> float:
    """% of the window's OWN values <= value, 0-100. Matches percentile_cont/rank-of-value
    semantics used throughout the card's own analysis (CC1859_ivp_window_and_bands.md)."""
    if not window:
        return 50.0
    n = len(window)
    le = sum(1 for v in window if v <= value)
    return round(100.0 * le / n, 1)


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return (s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0)


def atm_iv_history(cur, symbol: str) -> List[Tuple]:
    """[(trade_date, blended_atm_iv, spot, expiry, dte)] ordered oldest->newest, one row per
    session where BOTH legs' ATM IV clear the G3 floor/ceiling. 'ATM' = the strike nearest that
    session's own spot; every (symbol, trade_date) in option_iv_daily carries exactly one expiry
    (verified empirically — cc#1859 build note), so there is no expiry tie-break to make."""
    cur.execute("""
        SELECT trade_date, option_type, strike, iv, spot, expiry
        FROM option_iv_daily
        WHERE symbol = %s AND iv IS NOT NULL AND iv >= %s AND iv <= %s AND spot IS NOT NULL AND spot > 0
        ORDER BY trade_date
    """, (symbol, IV_FLOOR, IV_CEILING))
    by_day: Dict = {}
    for trade_date, ot, strike, iv, spot, expiry in cur.fetchall():
        d = by_day.setdefault(trade_date, {"spot": float(spot), "expiry": expiry, "legs": {}})
        cur_best = d["legs"].get(ot)
        dist = abs(float(strike) - float(spot))
        if cur_best is None or dist < cur_best[0]:
            d["legs"][ot] = (dist, float(iv))
    out = []
    for trade_date in sorted(by_day.keys()):
        d = by_day[trade_date]
        legs = d["legs"]
        if "CE" in legs and "PE" in legs:
            atm_iv = (legs["CE"][1] + legs["PE"][1]) / 2.0
            dte = (d["expiry"] - trade_date).days if d["expiry"] else None
            out.append((trade_date, atm_iv, d["spot"], d["expiry"], dte))
    return out


def ivp_and_fair_value(cur, symbol: str, spot_today: float, strike: float, T_years: float,
                        cp: str) -> Dict:
    """ATM-only IVP + Black-Scholes fair value at `strike` USING THE ATM SIGMA (no skew) — the
    S5/ATM-row case (level from the blend). Returns {ok, n_sessions, median_iv, ivp, band,
    fair_value, market_price:None (caller fills), window} or {ok: False, reason} below the floor."""
    from deriv_metrics import _bs_price
    hist = atm_iv_history(cur, symbol)
    if len(hist) < MIN_SESSIONS:
        return {"ok": False, "reason": f"only {len(hist)} sessions of history, need {MIN_SESSIONS}+"}
    window = [h[1] for h in hist[-WINDOW_SESSIONS:]]
    today_iv = window[-1]
    median_iv = _median(window)
    ivp = _percentile_rank(today_iv, window)
    fair_value = _bs_price(spot_today, strike, T_years, median_iv, cp)
    return {"ok": True, "n_sessions": len(window), "window": WINDOW_SESSIONS,
            "median_iv": round(median_iv, 4), "today_iv": round(today_iv, 4),
            "ivp": ivp, "band": _band(ivp),
            "fair_value": round(fair_value, 2) if fair_value else None}


def bucket_skew_history(cur, symbol: str) -> Dict[Tuple[str, str, int], List[Tuple]]:
    """{(expiry_class, option_type, moneyness_pct): [(trade_date, skew), ...]} — skew measured
    WITHIN ITS OWN LEG against that leg's own ATM IV, same session, same leg (R3, log 6312). Only
    rows clearing the G3 floor/ceiling enter the series. expiry_class: 'wk' DTE<=10, 'mo' DTE>10."""
    cur.execute("""
        SELECT trade_date, option_type, strike, iv, spot, expiry
        FROM option_iv_daily
        WHERE symbol = %s AND iv IS NOT NULL AND iv >= %s AND iv <= %s AND spot IS NOT NULL AND spot > 0
        ORDER BY trade_date
    """, (symbol, IV_FLOOR, IV_CEILING))
    by_day: Dict = {}
    for trade_date, ot, strike, iv, spot, expiry in cur.fetchall():
        d = by_day.setdefault(trade_date, {"spot": float(spot), "expiry": expiry, "rows": {}})
        d["rows"].setdefault(ot, []).append((float(strike), float(iv)))
    out: Dict[Tuple[str, str, int], List[Tuple]] = {}
    for trade_date in sorted(by_day.keys()):
        d = by_day[trade_date]
        spot, expiry, rows = d["spot"], d["expiry"], d["rows"]
        if expiry is None:
            continue
        dte = (expiry - trade_date).days
        eclass = "wk" if dte <= 10 else "mo"
        for ot, strikes in rows.items():
            atm_iv = min(strikes, key=lambda sv: abs(sv[0] - spot))[1]
            for strike, iv in strikes:
                bucket = round((strike / spot - 1.0) * 100)
                out.setdefault((eclass, ot, bucket), []).append((trade_date, iv - atm_iv))
    return out


def chain_tags(cur, symbol: str, spot: float, strikes: List[float],
                dte_today: int) -> Dict[Tuple[float, str], Dict]:
    """cc#2004 wiring: ONE history fetch + ONE bucket fetch per chain REQUEST, not per strike —
    a naive per-strike call to ivp_and_fair_value/strike_fair_tag would re-fetch the whole
    symbol's option_iv_daily history once per (strike, leg), ~40+ redundant fetches for a
    20-strike chain. This computes every cell from the SAME two in-memory fetches.

    Returns {(strike, 'CE'|'PE'): {'tag', 'fair_value', 'ivp'}} — 'tag' is None (never
    fabricated) wherever the ATM history or that bucket's own history is short of its floor
    (MIN_SESSIONS / BUCKET_MIN_SESSIONS, G1/G2) — cc#2004's own verify requires this: the dot
    must render empty/grey, not a guessed colour, when the gate is not met.

    The ATM strike (nearest to spot among `strikes`) is priced from the LEVEL alone (median ATM
    IV, ranked by its own IVP — the S5 case). Every other requested strike is priced from LEVEL +
    that bucket's own median skew, ranked by the BUCKET's own skew IVP — cc#1859's ruled method
    (log 6301/6312), same as strike_fair_tag, just batched for one request."""
    from deriv_metrics import _bs_price
    hist = atm_iv_history(cur, symbol)
    out: Dict[Tuple[float, str], Dict] = {}
    if len(hist) < MIN_SESSIONS:
        reason = f"only {len(hist)} session(s) of ATM history, need {MIN_SESSIONS}+"
        for K in strikes:
            for cp in ("CE", "PE"):
                out[(K, cp)] = {"tag": None, "fair_value": None, "ivp": None, "reason": reason}
        return out
    window = [h[1] for h in hist[-WINDOW_SESSIONS:]]
    today_atm_iv = window[-1]
    median_atm_iv = _median(window)
    atm_ivp = _percentile_rank(today_atm_iv, window)
    atm_strike = min(strikes, key=lambda s: abs(s - spot)) if strikes else None
    buckets = bucket_skew_history(cur, symbol)
    eclass = "wk" if dte_today <= 10 else "mo"
    T = dte_today / 365.0
    for K in strikes:
        for cp in ("CE", "PE"):
            if K == atm_strike:
                fair_value = _bs_price(spot, K, T, median_atm_iv, cp)
                out[(K, cp)] = {"tag": _band(atm_ivp), "ivp": atm_ivp,
                                 "fair_value": round(fair_value, 2) if fair_value else None}
                continue
            bucket = round((K / spot - 1.0) * 100)
            series = (buckets.get((eclass, cp, bucket)) or [])[-WINDOW_SESSIONS:]
            if len(series) < BUCKET_MIN_SESSIONS:
                out[(K, cp)] = {"tag": None, "fair_value": None, "ivp": None,
                                 "reason": f"bucket {eclass}/{cp}/{bucket:+d}% has {len(series)} "
                                           f"session(s), need {BUCKET_MIN_SESSIONS}+"}
                continue
            skews = [s[1] for s in series]
            median_skew = _median(skews)
            skew_ivp = _percentile_rank(skews[-1], skews)
            fair_iv = median_atm_iv + median_skew
            fair_value = _bs_price(spot, K, T, fair_iv, cp) if fair_iv and fair_iv > 0 else None
            out[(K, cp)] = {"tag": _band(skew_ivp), "ivp": skew_ivp,
                             "fair_value": round(fair_value, 2) if fair_value else None}
    return out


def strike_fair_tag(cur, symbol: str, spot_today: float, atm_median_iv: float,
                     strike: float, T_years: float, cp: str, dte_today: int) -> Dict:
    """One ATM+-N wing strike: fair IV = ATM median IV (the LEVEL, blended) + this bucket's own
    median skew WITHIN cp's leg (the SHAPE) over the trailing window; band from this bucket's OWN
    skew IVP (today's skew ranked against the bucket's own skew history) — same mechanism as the
    ATM case, applied to skew instead of raw IV, per the card's own evidence_it_carries_signal
    measurement (a bucket's skew genuinely moves session to session and ranks meaningfully).
    Returns {ok:False, reason} if the bucket is short of MIN_SESSIONS (G1/G2 — never fabricated)."""
    from deriv_metrics import _bs_price
    all_buckets = bucket_skew_history(cur, symbol)
    eclass = "wk" if dte_today <= 10 else "mo"
    bucket = round((strike / spot_today - 1.0) * 100)
    series = all_buckets.get((eclass, cp, bucket)) or []
    series = series[-WINDOW_SESSIONS:]
    if len(series) < BUCKET_MIN_SESSIONS:
        return {"ok": False, "reason": f"bucket {eclass}/{cp}/{bucket:+d}% has {len(series)} "
                                        f"session(s), need {BUCKET_MIN_SESSIONS}+"}
    skews = [s[1] for s in series]
    today_skew = skews[-1]
    median_skew = _median(skews)
    ivp = _percentile_rank(today_skew, skews)
    fair_iv = atm_median_iv + median_skew
    fair_value = _bs_price(spot_today, strike, T_years, fair_iv, cp) if fair_iv > 0 else None
    return {"ok": True, "n_sessions": len(series), "bucket_pct": bucket, "expiry_class": eclass,
            "median_skew": round(median_skew, 4), "fair_iv": round(fair_iv, 4),
            "ivp": ivp, "band": _band(ivp),
            "fair_value": round(fair_value, 2) if fair_value else None}
