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
  >>> SUPERSEDED by cc#2031 Phase C — see AMENDED cc#2031 below. Kept verbatim as the historical
  >>> record of what CC1859_bucket_coverage.md originally reported.

  SANITY GATE — G3, cc#1847: IV floor 3% / ceiling 150%, applied when building the HISTORY series
  (a row failing it never enters a median/percentile) — a floored or ceilinged IV is a solver
  failure (deep-ITM priced under intrinsic, or similar), not a real observation, matching the
  cc#1994 build's own floor-artifact finding on this identical table.

DOES NOT price or tag anything beyond ATM+-5 (F5/G2 for stocks: wing buckets with insufficient
depth simply carry no tag — this module never widens the band on its own). Does not touch
deriv_metrics.py's ORIGINAL Black-Scholes solver — _bs_price/_bs_iv stay byte-for-byte unchanged
(F2, "no second pricer" — other surfaces still read them as-is). cc#2031 ADDS deriv_metrics.
_b76_price/_b76_iv (Black-76, additive, same d1/d2 family, still "one pricer family") and this
module's OWN pricing now goes through those instead — see AMENDED cc#2031 below for why and what
changed.

AMENDED cc#2031 (15-Sep-2026, Fable's 13-Sep audit of this module + deriv_metrics.py +
option_iv_daily). Supersedes the PRICER/WING-TAG/BUCKETS pieces of THE METHOD above; the
LEVEL/SHAPE/WINDOW/BANDS ruling text above is preserved verbatim as the historical record and
stays in force except where this section overrides it. Full evidence + before/after proof:
reports/CC2031_option_black76_wingtag_buckets.md.

  PRICER — the original premise was wrong, not the maths: every fair_value here priced as if the
  underlying drifts at R_FREE (7%) to expiry (_bs_price's d1 has a (R_FREE+0.5*sigma^2)*T drift
  term). Real NIFTY/BANKNIFTY put-call parity implies an actual carry of 0.2-0.7%, never near 7%
  (cc#2031 e1_forward_error evidence — at DTE 18, ~79pt forward error, ~+-40 on an ATM leg worth
  ~225: the call ~18% too rich, the put ~18% too cheap, from the pricer alone, no skew involved).
  fair_value now prices via deriv_metrics._b76_price (Black-76, r for DISCOUNTING only, no drift
  term — the forward already embeds whatever carry produced it). chain_tags derives a genuine
  put-call-parity forward when both ATM legs are live-quoted (forward_source='parity'); every
  other case (chain_tags with no live quotes, strike_fair_tag, ivp_and_fair_value — neither of the
  latter two has a caller anywhere in this codebase today, kept correct for pricer-family
  consistency) falls back to F=spot*e^(R_FREE*T) (forward_source='carry_fallback' where that field
  exists) — ALGEBRAICALLY IDENTICAL to the old _bs_price(spot,...) call it replaces (proven in
  deriv_metrics._b76_price's own docstring), so nothing regresses when live quotes are
  unavailable; it only improves when they are.

  WING TAG — the skew-only percentile ignored the LEVEL: an OTM leg's tag ranked its skew alone
  against that bucket's own skew history, so a put could read CHEAP purely on a low skew
  percentile even while the whole vol surface (ATM included) sat at a 95th-percentile high — tag
  and fair_value could point opposite ways on the same cell (e2_wing_tag evidence). The TAG (not
  the price) now ranks the bucket's own TOTAL leg IV today — today_atm_leg_iv + today_skew, i.e.
  that bucket's own raw stored IV — within that SAME raw leg-iv series over the window. fair_iv
  (the price) is UNCHANGED: still LEVEL (median_atm_iv) + this bucket's median skew, exactly the
  original ruling — only what the percentile ranks against moved. ATM cell unchanged (S5 case).

  BUCKETS — two defects fixed together. (1) option_iv_daily carries only ONE expiry per (symbol,
  trade_date) today (monthly) — the wk/mo (DTE<=10 / >10) key split had nothing real to split ON,
  and instead starved every non-ATM tag for the last ~7 sessions of each monthly cycle (40
  sessions < BUCKET_MIN_SESSIONS 60; e3_wk_mo_starvation evidence). Dropped — key is now
  (option_type, sigma_bucket), no expiry_class. IF WEEKLY EXPIRIES ARE EVER LOADED into
  option_iv_daily alongside monthlies, THIS SPLIT MUST RETURN — merging a weekly and a monthly
  ATM at the same moneyness would blend two genuinely different distributions into one. (2)
  whole-percent-of-spot moneyness collapsed NIFTY's 50pt strike spacing (0.21% of spot) into one
  bucket across its entire ATM+-2 band, ranking a real strike against near-zero-variance noise
  (e4_bucket_collapse evidence). Moneyness is now sigma-units: z = ln(K/F) /
  (sigma_atm_session*sqrt(T_session)), bucket = round(z*2)/2 (half-sigma steps) — see
  _sigma_moneyness_bucket, the ONE function both history (bucket_skew_history) and today's live
  chain (chain_tags/strike_fair_tag) call, so history and today's chain always classify strikes
  into the identical grid (ONE_REGISTRY_ONE_DERIVATION_V1, session_log 33549 — the same principle
  deriv_metrics._price_rows already names). This bucket-assignment F is always the carry-based
  proxy (spot*e^(R_FREE*T)) — deliberately NEVER the live parity forward chain_tags prices with:
  option_iv_daily has no forward column to look a past session's real parity forward back up from
  (do_not_touch: no ALTER TABLE), and conflating "which bucket" with "how it's priced" would let
  the same word quietly answer two different, drifting questions. BUCKET_MIN_SESSIONS stays 60 —
  never lowered (G1/G2).
"""
import logging
import math
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


def _sigma_moneyness_bucket(K: float, F: float, sigma_atm: float, T: float) -> Optional[float]:
    """cc#2031 C2: half-sigma moneyness bucket, z = ln(K/F) / (sigma_atm*sqrt(T)), bucket =
    round(z*2)/2 — computed the SAME WAY for history rows (bucket_skew_history) and today's live
    chain (chain_tags/strike_fair_tag), ONE_REGISTRY_ONE_DERIVATION_V1. `F` here is deliberately
    the CARRY-based forward (spot*e^(R_FREE*T)) in every caller, historical and live alike — never
    the live parity forward chain_tags uses for actual PRICING (F_price) — a bucket only needs a
    stable, always-available moneyness proxy to classify strikes consistently across sessions; the
    parity forward is not stored per historical session (option_iv_daily carries no forward
    column, do_not_touch on ALTER TABLE), and re-deriving it per session would silently make
    "bucket" and "fair_value" answer two different, drifting questions with the same word. None
    (never a fabricated bucket) when sigma_atm or T cannot support the division — T<=0 is an
    expiry-day row/quote, sigma_atm<=0 should not happen post the G3 floor but is guarded anyway."""
    if not sigma_atm or sigma_atm <= 0 or not T or T <= 0 or not F or F <= 0 or not K or K <= 0:
        return None
    z = math.log(K / F) / (sigma_atm * math.sqrt(T))
    return round(z * 2) / 2.0


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
    """ATM-only IVP + Black-76 fair value at `strike` USING THE ATM SIGMA (no skew) — the
    S5/ATM-row case (level from the blend). Returns {ok, n_sessions, median_iv, ivp, band,
    fair_value, market_price:None (caller fills), window} or {ok: False, reason} below the floor.

    cc#2031 A1: priced via _b76_price with F=spot_today*e^(R_FREE*T_years) — this function has no
    live per-leg quote to derive a parity forward from, and is not called from anywhere in the
    codebase today (kept correct and consistent with chain_tags' pricer family rather than
    removed). F=S*e^(rT) is algebraically IDENTICAL to the old _bs_price call it replaces
    (deriv_metrics._b76_price's own docstring) — zero behaviour change here."""
    from deriv_metrics import _b76_price, R_FREE
    hist = atm_iv_history(cur, symbol)
    if len(hist) < MIN_SESSIONS:
        return {"ok": False, "reason": f"only {len(hist)} sessions of history, need {MIN_SESSIONS}+"}
    window = [h[1] for h in hist[-WINDOW_SESSIONS:]]
    today_iv = window[-1]
    median_iv = _median(window)
    ivp = _percentile_rank(today_iv, window)
    F = spot_today * math.exp(R_FREE * T_years) if spot_today and T_years and T_years > 0 else None
    fair_value = _b76_price(F, strike, T_years, median_iv, cp)
    return {"ok": True, "n_sessions": len(window), "window": WINDOW_SESSIONS,
            "median_iv": round(median_iv, 4), "today_iv": round(today_iv, 4),
            "ivp": ivp, "band": _band(ivp),
            "fair_value": round(fair_value, 2) if fair_value else None}


def bucket_skew_history(cur, symbol: str) -> Dict[Tuple[str, float], List[Tuple]]:
    """{(option_type, sigma_bucket): [(trade_date, skew, raw_iv), ...]} — skew measured WITHIN
    ITS OWN LEG against that leg's own ATM IV, same session, same leg (R3, log 6312). Only rows
    clearing the G3 floor/ceiling enter the series. Each entry also carries `raw_iv` (that leg's
    own stored IV that session — cc#2031 Phase B: the wing TAG now ranks this, not the skew alone;
    fair_value keeps ranking off `skew`, unchanged).

    cc#2031 C1/C2 (supersedes the original wk/mo + whole-percent-of-spot bucket scheme — see
    AMENDED cc#2031 in the module docstring for the full evidence). Key is now
    (option_type, half_sigma_bucket) — NO expiry_class split; IF WEEKLY EXPIRIES ARE EVER LOADED
    into option_iv_daily alongside monthlies, THIS SPLIT MUST RETURN. Moneyness bucketing goes
    through _sigma_moneyness_bucket, shared verbatim with chain_tags/strike_fair_tag's own
    bucketing of today's chain."""
    from deriv_metrics import R_FREE
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
    out: Dict[Tuple[str, float], List[Tuple]] = {}
    for trade_date in sorted(by_day.keys()):
        d = by_day[trade_date]
        spot, expiry, rows = d["spot"], d["expiry"], d["rows"]
        if expiry is None:
            continue
        dte = (expiry - trade_date).days
        if dte <= 0:
            continue   # expiry-day row: T=0 cannot support the sigma-unit bucket division (C2)
        T_session = dte / 365.0
        F_session = spot * math.exp(R_FREE * T_session)   # carry proxy — see _sigma_moneyness_bucket
        for ot, strikes in rows.items():
            atm_iv = min(strikes, key=lambda sv: abs(sv[0] - spot))[1]
            for strike, iv in strikes:
                bucket = _sigma_moneyness_bucket(strike, F_session, atm_iv, T_session)
                if bucket is None:
                    continue
                out.setdefault((ot, bucket), []).append((trade_date, iv - atm_iv, iv))
    return out


def chain_tags(cur, symbol: str, spot: float, strikes: List[float], dte_today: int,
               px: Optional[Dict[Tuple[float, str], float]] = None
               ) -> Tuple[Dict[Tuple[float, str], Dict], Optional[str]]:
    """cc#2004 wiring: ONE history fetch + ONE bucket fetch per chain REQUEST, not per strike —
    a naive per-strike call to ivp_and_fair_value/strike_fair_tag would re-fetch the whole
    symbol's option_iv_daily history once per (strike, leg), ~40+ redundant fetches for a
    20-strike chain. This computes every cell from the SAME two in-memory fetches.

    Returns ({(strike, 'CE'|'PE'): {'tag', 'fair_value', 'ivp'}}, forward_source) — 'tag' is None
    (never fabricated) wherever the ATM history or that bucket's own history is short of its floor
    (MIN_SESSIONS / BUCKET_MIN_SESSIONS, G1/G2) — cc#2004's own verify requires this: the dot must
    render empty/grey, not a guessed colour, when the gate is not met.

    The ATM strike (nearest to spot among `strikes`) is priced from the LEVEL alone (median ATM
    IV, ranked by its own IVP — the S5 case), unchanged by cc#2031. Every other requested strike
    is priced from LEVEL + that bucket's own median skew (cc#1859's ruled method), but TAGGED
    (cc#2031 Phase B) by that bucket's own TOTAL leg IV percentile, not the skew percentile.

    cc#2031 A/A3: `px` ({(strike,'CE'|'PE'): ltp}, optional) is today's live quotes, when the
    caller has them. When both legs at some strike are quoted, pricing uses the strike nearest
    spot among those as the parity anchor (forward_source='parity'); otherwise it falls back to
    F=spot*e^(R_FREE*T) (forward_source='carry_fallback') — algebraically identical to the old
    _bs_price(spot,...) call this replaces, so a caller with no live quotes sees the same numbers
    as before."""
    from deriv_metrics import _b76_price, R_FREE
    hist = atm_iv_history(cur, symbol)
    out: Dict[Tuple[float, str], Dict] = {}
    if len(hist) < MIN_SESSIONS:
        reason = f"only {len(hist)} session(s) of ATM history, need {MIN_SESSIONS}+"
        for K in strikes:
            for cp in ("CE", "PE"):
                out[(K, cp)] = {"tag": None, "fair_value": None, "ivp": None, "reason": reason}
        return out, None
    window = [h[1] for h in hist[-WINDOW_SESSIONS:]]
    today_atm_iv = window[-1]
    median_atm_iv = _median(window)
    atm_ivp = _percentile_rank(today_atm_iv, window)
    atm_strike = min(strikes, key=lambda s: abs(s - spot)) if strikes else None
    buckets = bucket_skew_history(cur, symbol)
    T = dte_today / 365.0

    # cc#2031 A3: the PRICING forward — parity when both legs near the money are live-quoted,
    # carry-fallback otherwise. Deliberately SEPARATE from F_bucket below (bucket-assignment
    # forward) — see _sigma_moneyness_bucket's docstring for why the two must not be conflated.
    F_price, forward_source = None, None
    if T > 0:
        best = None
        if px:
            for K in strikes:
                C, P = px.get((K, "CE")), px.get((K, "PE"))
                if C and C > 0 and P and P > 0:
                    dist = abs(K - spot)
                    if best is None or dist < best[0]:
                        best = (dist, K, C, P)
        if best is not None:
            _, K_atm_px, C_atm, P_atm = best
            F_price = K_atm_px + (C_atm - P_atm) * math.exp(R_FREE * T)
            forward_source = "parity"
        else:
            F_price = spot * math.exp(R_FREE * T)
            forward_source = "carry_fallback"
    # cc#2031 C2: the BUCKET-ASSIGNMENT forward — always carry-based, matching bucket_skew_
    # history's own per-session F exactly, so today's chain buckets into the SAME sigma grid its
    # own history was classified into.
    F_bucket = spot * math.exp(R_FREE * T) if T > 0 else None

    for K in strikes:
        for cp in ("CE", "PE"):
            if K == atm_strike:
                fair_value = _b76_price(F_price, K, T, median_atm_iv, cp)
                out[(K, cp)] = {"tag": _band(atm_ivp), "ivp": atm_ivp,
                                 "fair_value": round(fair_value, 2) if fair_value else None}
                continue
            bucket = _sigma_moneyness_bucket(K, F_bucket, today_atm_iv, T)
            if bucket is None:
                out[(K, cp)] = {"tag": None, "fair_value": None, "ivp": None,
                                 "reason": "T<=0 or bad inputs, no bucket"}
                continue
            series = (buckets.get((cp, bucket)) or [])[-WINDOW_SESSIONS:]
            if len(series) < BUCKET_MIN_SESSIONS:
                out[(K, cp)] = {"tag": None, "fair_value": None, "ivp": None,
                                 "reason": f"bucket {cp}/{bucket:+.1f}sigma has {len(series)} "
                                           f"session(s), need {BUCKET_MIN_SESSIONS}+"}
                continue
            skews = [s[1] for s in series]
            raw_ivs = [s[2] for s in series]
            median_skew = _median(skews)
            # cc#2031 Phase B (e2_wing_tag fix): TAG ranks the bucket's own TOTAL leg IV today
            # (today_atm_leg_iv + today_skew, i.e. the bucket's own raw stored iv) within that
            # SAME raw leg-iv series — not the skew alone. The old skew-only percentile ignored
            # the LEVEL entirely: an OTM put could read CHEAP purely because its skew sat at a low
            # percentile, even while the whole vol surface (ATM included) was at a 95th-percentile
            # high — tag and fair_value could point opposite ways on the identical cell. fair_iv
            # (the PRICE) is unchanged — still LEVEL + this bucket's median skew, per the ruling.
            today_raw_iv = raw_ivs[-1]
            tag_ivp = _percentile_rank(today_raw_iv, raw_ivs)
            fair_iv = median_atm_iv + median_skew
            fair_value = _b76_price(F_price, K, T, fair_iv, cp) if fair_iv and fair_iv > 0 else None
            out[(K, cp)] = {"tag": _band(tag_ivp), "ivp": tag_ivp,
                             "fair_value": round(fair_value, 2) if fair_value else None}
    return out, forward_source


def strike_fair_tag(cur, symbol: str, spot_today: float, atm_median_iv: float,
                     strike: float, T_years: float, cp: str, dte_today: int) -> Dict:
    """One ATM+-N wing strike: fair IV = ATM median IV (the LEVEL, blended) + this bucket's own
    median skew WITHIN cp's leg (the SHAPE) over the trailing window; TAG (cc#2031 Phase B) from
    this bucket's own TOTAL leg IV percentile today, ranked within that bucket's own raw leg-iv
    series — same mechanism as chain_tags' non-ATM cells, applied here as the single-strike case.
    Returns {ok:False, reason} if the bucket is short of MIN_SESSIONS (G1/G2 — never fabricated).

    cc#2031 A1: priced via _b76_price with F=spot_today*e^(R_FREE*T_years) — this function takes
    no live per-leg quotes (unlike chain_tags, it is not called from strike_chain() or anywhere
    else in the codebase today), so it cannot derive a parity forward; F=S*e^(rT) is algebraically
    IDENTICAL to the old _bs_price call it replaces, so this is a zero-behaviour-change swap kept
    for pricer-family consistency (a future live caller wiring real quotes in should extend this
    signature the same way chain_tags' `px` param does, not fork a third pricer path)."""
    from deriv_metrics import _b76_price, R_FREE
    all_buckets = bucket_skew_history(cur, symbol)
    F = spot_today * math.exp(R_FREE * T_years) if spot_today and T_years and T_years > 0 else None
    bucket = _sigma_moneyness_bucket(strike, F, atm_median_iv, T_years)
    if bucket is None:
        return {"ok": False, "reason": "T<=0 or bad inputs, no bucket"}
    series = all_buckets.get((cp, bucket)) or []
    series = series[-WINDOW_SESSIONS:]
    if len(series) < BUCKET_MIN_SESSIONS:
        return {"ok": False, "reason": f"bucket {cp}/{bucket:+.1f}sigma has {len(series)} "
                                        f"session(s), need {BUCKET_MIN_SESSIONS}+"}
    skews = [s[1] for s in series]
    raw_ivs = [s[2] for s in series]
    median_skew = _median(skews)
    today_raw_iv = raw_ivs[-1]
    ivp = _percentile_rank(today_raw_iv, raw_ivs)
    fair_iv = atm_median_iv + median_skew
    fair_value = _b76_price(F, strike, T_years, fair_iv, cp) if fair_iv > 0 else None
    return {"ok": True, "n_sessions": len(series), "bucket_sigma": bucket,
            "median_skew": round(median_skew, 4), "fair_iv": round(fair_iv, 4),
            "ivp": ivp, "band": _band(ivp),
            "fair_value": round(fair_value, 2) if fair_value else None}
