"""
Trade Check R6/R7 volume rules.

cc#1441 push 3 (VOLUME_METRICS_CANON_V2, session_log 33843): R6 (native trade check) and R7
(V4 endpoints) are now the canon 3-TIER read — r6_read() + r6_state() below. The linear
T-factor formula that used to be R6/R7 is RETAINED FURTHER DOWN as volume_ratio() ONLY because
the founder-locked V4-dual rulebook vol tests (cc#934 / session_log 18062: tc_v4_dual +
tc_v4_scan, thresholds locked on the T-factor scale) still read it — swapping the metric under
a locked threshold without sign-off would silently change locked card scores. Its full
retirement is a QUESTION in the cc#1441 task log; do not add new consumers.

THE CANON 3-TIER (R6/R7):
  RVOL   = live slot-normalized pace (rvol_engine.live_rvol — today's cumulative volume ÷ the
           21-session average cumulative volume at the same 5-min slot).
  VOL P  = the last completed session's closing RVOL (rvol_engine.eod_rvol_pair raw form —
           full universe, no profile row needed, per the profile build's anchor property).
  PASS   if BOTH clear their thresholds; WATCH if exactly one; FAIL if neither.
  Partial data never PASSes: with only one side known, clearing it earns WATCH, failing it
  FAILs; both unknown = no data (None), never fabricated.

── the LEGACY formula (RETIRED cc#1786 — kept here as history only) ──────────────────────────────────
FORMULA:
  Baseline     = AVG(raw_prices.volume) over the last 5 trading days (simple mean).
  T_factor     = elapsed_market_minutes / 375  (market_start=09:15 IST, full day=375min).
  Expected_vol = Baseline * T_factor.
  Today_vol    = source-agnostic + semantics-aware today volume (see cc#150 below).
  Ratio        = Today_vol / Expected_vol.

Thresholds (same for LONG and SHORT -- high volume confirms conviction either way):
  ratio >  1.2        -> PASS  (1.0)
  1.0 <= ratio <= 1.2  -> WATCH (0.5)
  ratio <  1.0         -> FAIL  (0.0)

After market close (or outside market hours): fallback = raw_prices today volume
(if the EOD row already exists) / Baseline, no T_factor.

CALIBRATION NOTE (cc#145): the spec's "Today_vol = SUM(intraday_prices.volume)"
wording would double-count -- intraday_prices.volume is a CUMULATIVE running
total per bar (verified: values strictly increase through the day for a given
symbol/day), not a per-5-min increment. Summing 22 already-cumulative bars for
CGPOWER on 01-Jul produced ~29M, matching the task's flagged anomaly exactly --
this is a SUM-of-cumulative artifact, not a genuine fyers_eq/raw_prices scale
mismatch (confirmed: CGPOWER Jun30 raw_prices=3,297,252 is in the same scale as
fyers_eq ticks). Fix: Today_vol = the LATEST tick's volume (already cumulative
to that point), not SUM across bars. No normalization factor needed.

cc#150 (02-Jul-2026, fault_A/B): filtering strictly on source='fyers_eq' matched
only ~13 symbols (the main live stream tags most equity ticks source='fyers'),
so ratio came back None for ~95% of the futures universe during market hours.
Worse, 'fyers'-tagged rows in intraday_prices are PER-BAR volume (not cumulative
like fyers_eq), so a blanket "latest tick" read would silently under-count for
those symbols. Fix (OPTION B, locked): accept source IN ('fyers','fyers_eq'),
dedupe same-bucket rows preferring fyers_eq, then auto-detect cumulative vs
per-bar semantics per symbol/day from today's sampled bars (monotonic
non-decreasing -> cumulative -> Today_vol=latest; else per-bar -> Today_vol=SUM).
Immune to future source/semantics drift by construction.
"""

from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# cc#1786 (TC_VOLUME_SIMPLE_SELL_V1, 39692): the legacy T-factor volume_ratio() and the helpers
# only it used (_baseline_5d, _today_volume_rows, _detect_today_vol, _is_market_hours, _ist_now,
# the market-clock constants) are RETIRED. Their last readers were the locked 18062 vol tests in
# tc_v4_dual / tc_v4_scan (LOCK_VOLUME on the SELL cards, the old R5 on the BUY cards); cc#1785 and
# cc#1786 replaced both with the four-check R5 / R5V on the canon reads, so no caller remains
# (grep: tc_score_replay carries its own as-of function and never called this one). Retired in
# the same push that removed the last reader, per the card; do not reintroduce.

# ── cc#1441 push 3: the canon R6/R7 read + 3-tier ────────────────────────────────────────────
# Thresholds FOUNDER-SIGNED FINAL 30-Aug-2026 (cc_task_logs, task 1441): PASS/WATCH/FAIL lands
# 12.3 / 26.2 / 61.6 over 30 sessions x full universe. Change only via a new sign-off.
R6_RVOL_X = 1.2   # FINAL — founder-signed 30-Aug-2026 (cc#1441)
R6_VOLP_Y = 1.0   # FINAL — founder-signed 30-Aug-2026 (cc#1441)


def r6_read(cur, symbol: str) -> dict:
    """Canon volume read: {'rvol', 'vol_p', 'vol_p_asof', 'partial'}. rvol = live profile read
    (None when the symbol has no rvol_profile with enough sessions — never fabricated). vol_p =
    the closing RVOL of the session immediately BEFORE the one rvol anchors to (cc#1449 ruling:
    the pair must always be two DIFFERENT sessions). The raw eod pair's latest row is yesterday
    during a live session but the JUST-CLOSED session off-market — so when its date equals the
    live anchor's, step back to the pair's LAG side. Both derivations live in rvol_engine."""
    from rvol_engine import live_rvol, eod_rvol_pair
    lv = live_rvol(cur, symbol)
    rvol = lv.get("rvol") if lv else None
    anchor = lv.get("asof") if lv else None
    pair = eod_rvol_pair(cur, symbol) or {}
    if pair.get("asof") is not None and anchor is not None and str(pair["asof"]) == str(anchor):
        vol_p, vol_p_asof = pair.get("vol_p"), pair.get("prev_asof")
    else:
        vol_p, vol_p_asof = pair.get("rvol"), pair.get("asof")
    return {"rvol": rvol, "vol_p": vol_p, "vol_p_asof": vol_p_asof,
            "partial": (rvol is None) != (vol_p is None),
            # cc#1785 (39713 vol_r_early_guard): the live read's slot flags, passed through as
            # live_rvol reports them — early = slot before 09:30 (EARLY_SLOTS), closed = off-market
            # or a stale day. Additive; the 3-tier readers above ignore them.
            "rvol_slot": (lv or {}).get("slot"), "rvol_early": bool((lv or {}).get("early")),
            "rvol_closed": bool((lv or {}).get("closed"))}


def r6_read_batch(cur, symbols) -> dict:
    """cc#1978 MARKER_TICKS_V1: batch form of r6_read for the {rvol, vol_p} pair ONLY — the two
    fields evaluate_activity() (the only caller a universe-wide tick needs this for) actually
    reads; NOT full parity with r6_read's dict (partial/rvol_slot/rvol_early/rvol_closed are not
    reproduced here — no caller of this batch form needs them; use r6_read itself if they matter).

    Composes two EXACT batch primitives, not an approximation: live_rvol_batch (already shipped,
    rvol_engine.py) for the live/rvol leg, and eod_rvol_pair_batch (added by this card, same file
    — a true PARTITION BY rewrite of eod_rvol_pair's own SQL, verified byte-identical to it on
    real data) for the EOD pair. Reproduces r6_read's exact branching for vol_p: when the EOD
    pair's own latest session equals the live anchor (today's close has already landed), read the
    pair's LAG side (vol_p/prev_asof); otherwise read the pair's own latest (rvol/asof) directly.

    ONE APPROXIMATION, inherited rather than introduced: live_rvol_batch anchors every symbol to
    the SAME latest session date (one _sessions(cur,1) call for the whole batch), where
    single-symbol live_rvol resolves each symbol's own latest intraday date independently. This
    batch form uses that same shared anchor for the vol_p branch test too — already the accepted
    convention of live_rvol_batch itself (in production use elsewhere before this card), not a
    new approximation this card adds.

    Returns {symbol: {'rvol': float-or-None, 'vol_p': float-or-None}}."""
    from rvol_engine import live_rvol_batch, eod_rvol_pair_batch, _sessions
    symbols = [(s or "").upper() for s in symbols]
    if not symbols:
        return {}
    rvol_map = live_rvol_batch(cur, symbols)
    days = _sessions(cur, 1)
    anchor = str(days[0]) if days else None
    pair_map = eod_rvol_pair_batch(cur, symbols)
    out = {}
    for sym in symbols:
        pair = pair_map.get(sym) or {}
        if pair.get("asof") is not None and anchor is not None and str(pair["asof"]) == anchor:
            vol_p = pair.get("vol_p")
        else:
            vol_p = pair.get("rvol")
        out[sym] = {"rvol": rvol_map.get(sym), "vol_p": vol_p}
    return out


def r6_state(vr):
    """3-tier on the r6_read dict: True=PASS (both clear), 'watch'=exactly one clears,
    False=neither, None=no data at all. Same for LONG/SHORT — participation confirms either
    way. Partial data never PASSes (see module docstring)."""
    if not vr:
        return None
    rv, vp = vr.get("rvol"), vr.get("vol_p")
    if rv is None and vp is None:
        return None
    rv_hit = rv is not None and rv >= R6_RVOL_X
    vp_hit = vp is not None and vp >= R6_VOLP_Y
    if rv is not None and vp is not None:
        return True if (rv_hit and vp_hit) else ("watch" if (rv_hit or vp_hit) else False)
    return "watch" if (rv_hit or vp_hit) else False


def r6_label(vr) -> str:
    if not vr or (vr.get("rvol") is None and vr.get("vol_p") is None):
        return "RVOL —"
    rv, vp = vr.get("rvol"), vr.get("vol_p")
    a = f"x{rv:.2f}" if rv is not None else "—"
    b = f"x{vp:.2f}" if vp is not None else "—"
    return f"RVOL {a} · prev {b}"
