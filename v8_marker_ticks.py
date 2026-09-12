"""
v8_marker_ticks.py — cc#1978 MARKER_TICKS_STATE_V1 (founder go 10-Sep-2026 ~18:10 IST;
DDL amended per Fable's ruling, cc_task_logs task_id=1199 id=6202; founder DDL approval,
cc_task_logs 1199 id=6207).

Persists the marker evaluation STATE the app already computes every tick and currently throws
away, for the families where the SOURCE COMPUTE already covers the full registry universe.
This is PERSISTENCE, not new evaluation — session_log 41629's THE_LEARNING, quoted on the card:
"the work is already being done and the output is thrown away."

ONE STATE ROW per (symbol, ts, family): `fired` boolean, so "evaluated and did not fire" and
"never evaluated this tick" are never confused (Fable's ruling, log 6202 — the same reasoning
tc_universe_ticks already applies). A family with NO row for a symbol at a tick means that
family did not run for that symbol at that tick; that absence is the whole point of the card.

Table (already created, founder-approved, log 6207):
  v8_marker_ticks(symbol, ts, family, fired, direction, colour, level_name, level_value,
                  detail jsonb, PRIMARY KEY(symbol, ts, family))

FAMILY NAMING — a deliberate compound scheme, not a schema change. CHAN's sell/buy triggers and
TCS's four buckets are each independent per-tick truths that can co-occur for the same symbol at
the same tick (e.g. a symbol can trip both CHAN's sell touch AND its buy touch inside one 3-day
window's regression; a symbol can be amber on BUY-MOM and SELL-REV at once). The 3-column PK
(symbol, ts, family) has no room for a fourth axis, so each independent truth gets its own family
value instead of a schema change (no ALTER TABLE, MAINTENANCE_LOCK_RULE cc#351):
  chan_sell, chan_buy                                  — CHAN's two independent triggers
  tcs_buy_mom, tcs_buy_rev, tcs_sell_mom, tcs_sell_rev  — TCS's four tc_universe_ticks buckets

FIVE families, across two builds:
  CHAN  reads v8_channel_5m.compute_channels()'s in-memory `fits` dict — already computed for the
        FULL active-futures universe every 5-min tick. No re-evaluation, no second compute path;
        the caller (v8_channel_5m.run_tick) passes `fits` straight through after its own use.
  TCS   reads tc_universe_ticks (already all 207 symbols, all 4 buckets, cc#1862 /
        TC_CANON_V2_FINAL) and applies the SAME amber condition v8_pivot_star.run_tc_score_tick()
        already applies to the book (TC_STRONG_PCT / TC_TRAIL_DAYS, imported not retyped): the
        latest tick's score100 must exceed TC_STRONG_PCT AND exceed the 3-day trailing average of
        that bucket's own daily-last-tick series. One extra read per bucket for the trailing
        average — never a second scorer, never a re-run of Trade Check.
  STAR  cc#1978 items 5/8 (12-Sep, sha 616d1b1 + this push): v8_pivot_star.EVAL_SCOPE="positions"
        (session_log 18052) is a FOUNDER LOCK on the book-scoped marker — untouched. The universe
        reading is authorized separately and explicitly (FOUNDER_WORD_10SEP_2205_STARS_UNIVERSE,
        quoted verbatim in cc#1978's own spec, reconfirmed 12-Sep log 6382), and lands as NEW
        functions (evaluate_universe/evaluate_universe_with_state) sharing the book-scoped
        marker's own condition math via extracted helpers — never a flag flip on EVAL_SCOPE,
        never a duplicated copy of the rule. persist_star_ticks() below calls the universe path
        only; evaluate()/EVAL_SCOPE behave exactly as before this card, proven by isolated test.
  DMA   same authorization, same shared-helper shape: evaluate_dma_state_universe() reuses
        evaluate_dma_state()'s own extracted helpers (that function never read EVAL_SCOPE to
        begin with). Unlike STAR, DMA STATE has no "ran, no state" outcome — every symbol with
        enough closing history gets a definite GREEN or RED every tick, so persist_dma_ticks()
        writes fired=true for every row it gets; a symbol absent from the universe read (short
        history, or the rare exact tie) simply gets no row, same as any other family's "did not
        run" convention.
  ACT   cc#1978 items 6/8: v8_pivot_star.evaluate_activity() loops r6_read/_ad_21d per symbol —
        the cc#1977 census measured that at ~50-60s for 208 symbols, unusable on a 5-min beat.
        Batched via three new functions (r6_read_batch, eod_rvol_pair_batch, _ad_21d_batch — each
        verified byte-identical to its single-symbol original on real data before use) composed
        into evaluate_activity_universe_with_state(), which reuses evaluate_activity()'s own
        extracted _score_activity_one() condition math. A universe symbol carries no position
        side (that's v8_paper_positions', not futures_universe's), so BOTH sides are scored per
        symbol — the same compound-family shape CHAN already uses (chan_sell/chan_buy: one
        compute, two independent per-tick truths that can co-occur): act_buy / act_sell below.

Retention: 30 days rolling, purged on the day's LAST tick each family runs on — same "ship the
purge with the writer" convention as tc_universe_ticks.py / v8_channel_5m.py. One shared _purge()
covers every family in this table (a plain age-based DELETE, not family-scoped), so no change was
needed there when STAR/DMA/ACT were added.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
import pytz
from fastapi import APIRouter

log = logging.getLogger("scorr.v8_marker_ticks")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
IST = pytz.timezone("Asia/Kolkata")

RETENTION_DAYS = 30   # founder ruling, cc_task_logs 1199/6202 sizing note

_BUCKET_FAMILY = {
    "BUY-MOM": "tcs_buy_mom", "BUY-REV": "tcs_buy_rev",
    "SELL-MOM": "tcs_sell_mom", "SELL-REV": "tcs_sell_rev",
}


def _conn():
    return psycopg2.connect(_DB)


def _ist_now():
    return datetime.now(IST)


def ensure_schema(conn):
    """CREATE TABLE IF NOT EXISTS only — mirrors the founder-approved DDL exactly (cc_task_logs
    1199/6207); a no-op against the live table. Never an ALTER from this codepath
    (MAINTENANCE_LOCK_RULE cc#351)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS v8_marker_ticks (
              symbol TEXT NOT NULL,
              ts TIMESTAMPTZ NOT NULL,
              family TEXT NOT NULL,
              fired BOOLEAN NOT NULL DEFAULT false,
              direction TEXT,
              colour TEXT,
              level_name TEXT,
              level_value NUMERIC,
              detail JSONB,
              PRIMARY KEY (symbol, ts, family)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS v8_marker_ticks_ts_idx ON v8_marker_ticks (ts DESC, symbol)")
    conn.commit()


def _purge(conn) -> Optional[int]:
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM v8_marker_ticks WHERE ts < NOW() - INTERVAL '%s days'" % RETENTION_DAYS)
            purged = cur.rowcount
        conn.commit()
        return purged
    except Exception as e:
        log.warning("v8_marker_ticks purge failed (non-fatal, next tick retries): %s", e)
        return None


def _is_last_tick(now=None) -> bool:
    """Same 15:20 IST boundary bg_pivot_star / bg_channel_5m / tc_universe_ticks.run_tick() all
    purge on — the established "ship the purge on the day's last tick" convention."""
    now = now or _ist_now()
    return now.hour == 15 and now.minute == 20


# ── CHAN — reads v8_channel_5m.compute_channels()'s in-memory fits, zero new compute ───────────
def persist_chan_ticks(conn, fits: Dict[str, Any], purge_now: Optional[bool] = None) -> Dict[str, Any]:
    """`fits` is compute_channels()'s own return value, keyed by symbol, already computed for the
    FULL active-futures universe this tick. Writes TWO state rows per symbol that has a fit —
    chan_sell and chan_buy — every tick, fired or not, so absence still means "did not run" and a
    row with fired=false means "ran, condition not met". Never touches v8_channel_5m.py's own
    write of v8_channel_5m or v8_pivot_star_log (evaluate_triangle_markers is untouched — do_not_touch,
    cc#1978 card)."""
    if not fits:
        return {"ok": True, "rows": 0, "note": "no fits this tick — valid empty tick"}
    ts = _ist_now()
    written = 0
    with conn.cursor() as cur:
        for sym, fit in fits.items():
            sell = fit.get("sell_trigger")
            buy = fit.get("buy_trigger")
            latest_ts = fit.get("latest_ts") or ts
            for family, trig, colour in (("chan_sell", sell, "GREEN"), ("chan_buy", buy, "GREEN")):
                fired = bool(trig)
                detail = {
                    "n_bars": fit.get("n_bars"), "upper_today": fit.get("upper_today"),
                    "lower_today": fit.get("lower_today"), "latest_close": fit.get("latest_close"),
                }
                if trig:
                    detail["touch_ts"] = str(trig.get("touch_ts"))
                    detail["touch_close"] = trig.get("touch_close")
                    detail["pct_move"] = trig.get("pct_move")
                cur.execute("""
                    INSERT INTO v8_marker_ticks
                      (symbol, ts, family, fired, direction, colour, level_name, level_value, detail)
                    VALUES (%s,%s,%s,%s,%s,%s,'CHAN_REJECT',%s,%s::jsonb)
                    ON CONFLICT (symbol, ts, family) DO NOTHING
                """, (sym, latest_ts, family, fired,
                      "SELL" if family == "chan_sell" else "BUY", colour if fired else None,
                      round(trig["pct_move"], 2) if trig else None, json.dumps(detail, default=str)))
                written += cur.rowcount
    conn.commit()
    do_purge = purge_now if purge_now is not None else _is_last_tick()
    purged = _purge(conn) if do_purge else None
    return {"ok": True, "symbols": len(fits), "rows_written": written, "purged": purged}


# ── TCS — reads tc_universe_ticks, applies the existing amber condition, zero new compute ──────
def persist_tcs_ticks(conn=None, purge_now: Optional[bool] = None) -> Dict[str, Any]:
    """One tick: read the LATEST tc_universe_ticks tick per (symbol, bucket) for today, join the
    3-day trailing average of that bucket's own daily-last-tick series (same JOIN LATERAL shape
    v8_pivot_star.run_tc_score_tick() already uses), write one v8_marker_ticks row per
    (symbol, bucket) — fired when score100 > TC_STRONG_PCT AND score100 > the trailing average,
    same threshold constants imported from v8_pivot_star, never retyped. Every (symbol, bucket)
    present in today's latest tick gets a row, fired or not — a bucket with <3 prior days of
    history is written with fired=false and a note, never a fabricated amber."""
    from v8_pivot_star import TC_STRONG_PCT, TC_TRAIL_DAYS
    own = conn is None
    if own:
        conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (symbol, bucket) symbol, bucket, ts, score100
                    FROM tc_universe_ticks
                    WHERE ts::date = (SELECT max(ts::date) FROM tc_universe_ticks)
                    ORDER BY symbol, bucket, ts DESC
                )
                SELECT l.symbol, l.bucket, l.ts, l.score100, h.trail_avg, h.n_prior
                FROM latest l
                JOIN LATERAL (
                    SELECT AVG(rep) AS trail_avg, COUNT(*) AS n_prior
                    FROM (SELECT DISTINCT ON (ts::date) score100 AS rep
                          FROM tc_universe_ticks
                          WHERE symbol = l.symbol AND bucket = l.bucket AND ts::date < l.ts::date
                          ORDER BY ts::date DESC, ts DESC
                          LIMIT %s) p
                ) h ON TRUE
            """, (TC_TRAIL_DAYS,))
            rows = cur.fetchall()
        written, amber = 0, 0
        latest_ts = None
        with conn.cursor() as cur:
            for sym, bucket, ts, score100, trail_avg, n_prior in rows:
                latest_ts = ts
                family = _BUCKET_FAMILY.get(bucket)
                if not family:
                    continue   # unknown bucket name — never guess a family, skip and let it surface elsewhere
                score100 = float(score100) if score100 is not None else None
                trail_avg = float(trail_avg) if trail_avg is not None else None
                enough_history = n_prior is not None and n_prior >= TC_TRAIL_DAYS
                fired = bool(enough_history and score100 is not None and trail_avg is not None
                             and score100 > TC_STRONG_PCT and score100 > trail_avg)
                if fired:
                    amber += 1
                detail = {"score100": score100, "trail_avg": trail_avg, "n_prior_days": n_prior,
                          "enough_history": enough_history}
                cur.execute("""
                    INSERT INTO v8_marker_ticks
                      (symbol, ts, family, fired, direction, colour, level_name, level_value, detail)
                    VALUES (%s,%s,%s,%s,%s,%s,'tc_score100',%s,%s::jsonb)
                    ON CONFLICT (symbol, ts, family) DO NOTHING
                """, (sym, ts, family, fired,
                      "BUY" if bucket.startswith("BUY") else "SELL",
                      "AMBER" if fired else None, score100, json.dumps(detail, default=str)))
                written += cur.rowcount
        conn.commit()
        do_purge = purge_now if purge_now is not None else _is_last_tick()
        purged = _purge(conn) if do_purge else None
        return {"ok": True, "pairs": len(rows), "rows_written": written, "amber": amber,
                "latest_ts": str(latest_ts) if latest_ts else None, "purged": purged,
                "zero_tick": not rows}
    except Exception as e:
        log.exception("v8_marker_ticks TCS tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


# ── STAR — cc#1978 items 5/8: reuses v8_pivot_star's universe-scope evaluation layer ────────────
def persist_star_ticks(conn=None, purge_now: Optional[bool] = None) -> Dict[str, Any]:
    """cc#1978 items 5/8: STAR marker state for the FULL active futures registry. Calls
    v8_pivot_star.evaluate_universe_with_state() (sha 616d1b1) — the SAME condition math
    evaluate()/the book-scoped marker uses, reused via a shared helper, not a duplicated copy
    (this module's own docstring names that constraint). EVAL_SCOPE and the book-scoped star are
    untouched by this call — evaluate_universe_with_state() is a completely separate code path
    from evaluate(), which alone reads EVAL_SCOPE.

    Writes ONE 'star' row per symbol in evaluated_syms per tick: fired=true with the star's own
    direction/colour/level when a star actually fired, fired=false (direction/colour/level all
    None) when the symbol cleared every data gate but did not fire. A symbol NOT in evaluated_syms
    (missing pivot, metrics or a live CMP) gets NO row at all — "never evaluated" stays
    distinguishable from "evaluated, no star", the same contract the star module's own
    evaluate_universe_with_state() docstring states."""
    from v8_pivot_star import evaluate_universe_with_state
    own = conn is None
    if own:
        conn = _conn()
    try:
        fired, evaluated_syms = evaluate_universe_with_state(conn)
        by_sym = {r["symbol"]: r for r in fired}
        ts = _ist_now()
        written = 0
        with conn.cursor() as cur:
            for sym in evaluated_syms:
                r = by_sym.get(sym)
                fired_bool = r is not None
                detail = ({"pct_from_level": r.get("pct_from_level"), "near_pp": r.get("near_pp"),
                           "day_1d": r.get("day_1d"), "mom_2d": r.get("mom_2d"),
                           "dma_50": r.get("dma_50"), "touched_dates": r.get("touched_dates")}
                          if r else {})
                cur.execute("""
                    INSERT INTO v8_marker_ticks
                      (symbol, ts, family, fired, direction, colour, level_name, level_value, detail)
                    VALUES (%s,%s,'star',%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (symbol, ts, family) DO NOTHING
                """, (sym, ts, fired_bool,
                      r.get("direction") if r else None,
                      r.get("star_color") if r else None,
                      r.get("level_name") if r else None,
                      r.get("level_value") if r else None,
                      json.dumps(detail, default=str)))
                written += cur.rowcount
        conn.commit()
        do_purge = purge_now if purge_now is not None else _is_last_tick()
        purged = _purge(conn) if do_purge else None
        return {"ok": True, "evaluated": len(evaluated_syms), "fired": len(fired),
                "rows_written": written, "purged": purged, "zero_tick": not evaluated_syms}
    except Exception as e:
        log.exception("v8_marker_ticks STAR tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


# ── DMA — cc#1978 items 5/8: state, not cross; every row written is fired=true ─────────────────
def persist_dma_ticks(conn=None, purge_now: Optional[bool] = None) -> Dict[str, Any]:
    """cc#1978 items 5/8: DMA STATE marker for the FULL active futures registry via
    v8_pivot_star.evaluate_dma_state_universe() (sha 616d1b1) — the same _fetch_dma_state_support/
    _score_dma_state helpers evaluate_dma_state() (the book-scoped marker) already uses, so the
    universe reading is the identical rule over a wider candidate set, never a duplicated copy.

    Unlike CHAN/TCS/STAR, DMA STATE has no "ran, no state" outcome for a symbol with enough
    history — every returned row is a definite GREEN (DMA_ABOVE) or RED (DMA_BELOW), so every row
    here is written fired=true. A symbol absent from evaluate_dma_state_universe()'s output
    (< DMA_SLOW completed closes, or the rare exact tie) gets no row at all — could not be
    evaluated this tick, the same "no row = did not run" convention every other family in this
    store uses; there is no meaningful fired=false state to invent for DMA."""
    from v8_pivot_star import evaluate_dma_state_universe
    own = conn is None
    if own:
        conn = _conn()
    try:
        rows = evaluate_dma_state_universe(conn)
        ts = _ist_now()
        written = 0
        with conn.cursor() as cur:
            for r in rows:
                detail = {"pp_20dma": r.get("pp"), "day_1d": r.get("day_1d"),
                          "data_date": str(r["data_date"]) if r.get("data_date") else None}
                cur.execute("""
                    INSERT INTO v8_marker_ticks
                      (symbol, ts, family, fired, direction, colour, level_name, level_value, detail)
                    VALUES (%s,%s,'dma',%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (symbol, ts, family) DO NOTHING
                """, (r["symbol"], ts, True, r.get("direction"), r.get("star_color"),
                      r.get("level_name"), r.get("level_value"), json.dumps(detail, default=str)))
                written += cur.rowcount
        conn.commit()
        do_purge = purge_now if purge_now is not None else _is_last_tick()
        purged = _purge(conn) if do_purge else None
        return {"ok": True, "symbols": len(rows), "rows_written": written, "purged": purged,
                "zero_tick": not rows}
    except Exception as e:
        log.exception("v8_marker_ticks DMA tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


# ── ACT — cc#1978 items 6/8: batched, no position side so both act_buy/act_sell are written ─────
def persist_act_ticks(conn=None, purge_now: Optional[bool] = None) -> Dict[str, Any]:
    """cc#1978 items 6/8: ACT marker (Vol R/P/D/AD, >=2-of-4 canon tally) for the FULL active
    futures registry, batched (the card's own cost gate — the per-symbol r6_read/_ad_21d loop
    measured ~50-60s at 208 symbols per the cc#1977 census, unusable on a 5-min beat). Calls
    v8_pivot_star.evaluate_activity_universe_with_state() (new, this push) — batched reads
    (r6_read_batch/deliv_ratio_batch/_ad_21d_batch), identical >=2-of-4 condition math
    evaluate_activity() itself now applies via the shared _score_activity_one() helper.

    A universe symbol carries no position side (that's v8_paper_positions' own field, not
    futures_universe's) — so BOTH sides are scored per symbol, same compound-family pattern this
    card's CHAN family already established (chan_sell/chan_buy: one compute, two independent
    per-tick truths that can co-occur): family='act_buy' tests the Accumulation reading,
    family='act_sell' tests the Distribution reading. Every symbol in the registry gets BOTH rows
    every tick, fired true or false — evaluate_activity_universe_with_state() scores every
    candidate regardless of data completeness (a None-valued read just fails its own check,
    exactly how the book-scoped evaluate_activity() already treats missing data), so there is no
    "insufficient data, no row" exclusion for this family the way STAR/DMA have — stated plainly
    since it is a deliberate difference, not an oversight."""
    from v8_pivot_star import evaluate_activity_universe_with_state
    own = conn is None
    if own:
        conn = _conn()
    try:
        _fired, all_rows = evaluate_activity_universe_with_state(conn)
        ts = _ist_now()
        written = 0
        with conn.cursor() as cur:
            for r in all_rows:
                family = "act_buy" if r["side"] == "BUY" else "act_sell"
                detail = {"checks": r.get("checks"), "checks_passed": r.get("checks_passed")}
                cur.execute("""
                    INSERT INTO v8_marker_ticks
                      (symbol, ts, family, fired, direction, colour, level_name, level_value, detail)
                    VALUES (%s,%s,%s,%s,%s,%s,'CHECKS_PASSED',%s,%s::jsonb)
                    ON CONFLICT (symbol, ts, family) DO NOTHING
                """, (r["symbol"], ts, family, r["fired"], r["side"],
                      r.get("star_color"), r.get("checks_passed"), json.dumps(detail, default=str)))
                written += cur.rowcount
        conn.commit()
        do_purge = purge_now if purge_now is not None else _is_last_tick()
        purged = _purge(conn) if do_purge else None
        fired_ct = sum(1 for r in all_rows if r["fired"])
        return {"ok": True, "symbols": (len(all_rows) // 2) if all_rows else 0,
                "rows_written": written, "fired": fired_ct, "purged": purged,
                "zero_tick": not all_rows}
    except Exception as e:
        log.exception("v8_marker_ticks ACT tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def read_symbol(conn, symbol: str, families: Optional[List[str]] = None) -> Dict[str, Any]:
    """Latest row per family for one symbol — the distribution read cc#1978 item 9 wants (per-
    symbol, any symbol, not just the open book). Returns {} for a symbol with no rows at all
    (never evaluated any family stored here yet) rather than fabricating a shape."""
    with conn.cursor() as cur:
        if families:
            cur.execute("""
                SELECT DISTINCT ON (family) family, ts, fired, direction, colour, level_name,
                       level_value, detail
                FROM v8_marker_ticks WHERE symbol=%s AND family = ANY(%s)
                ORDER BY family, ts DESC""", (symbol, families))
        else:
            cur.execute("""
                SELECT DISTINCT ON (family) family, ts, fired, direction, colour, level_name,
                       level_value, detail
                FROM v8_marker_ticks WHERE symbol=%s
                ORDER BY family, ts DESC""", (symbol,))
        out = {}
        for family, ts, fired, direction, colour, level_name, level_value, detail in cur.fetchall():
            out[family] = {"ts": str(ts), "fired": fired, "direction": direction, "colour": colour,
                            "level_name": level_name,
                            "level_value": float(level_value) if level_value is not None else None,
                            "detail": detail}
    return out


# ── cc#1978 item 9: DISTRIBUTION — any symbol, not just the open book ───────────────────────────
# New, additive route. Does not touch /api/v8/pivot_star's existing book-shaped response at all —
# unblocks cc#1976 (Check Flags on the A card) for a symbol outside the open book, per that card's
# own note that this store is what closes the gap.
@router.get("/api/v8/marker_ticks/{symbol}")
def marker_ticks_symbol(symbol: str):
    """Latest v8_marker_ticks row per family for one symbol. Empty families dict means this
    symbol has no rows in the store yet for the families currently written here (chan_sell,
    chan_buy, tcs_buy_mom, tcs_buy_rev, tcs_sell_mom, tcs_sell_rev, star, dma, act_buy, act_sell)
    — never a fabricated state."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"symbol": symbol, "families": {}, "error": "symbol required"}
    try:
        conn = _conn()
        try:
            ensure_schema(conn)
            families = read_symbol(conn, sym)
        finally:
            conn.close()
        return {"symbol": sym, "families": families,
                "families_covered": sorted(_BUCKET_FAMILY.values())
                                     + ["chan_sell", "chan_buy", "star", "dma", "act_buy", "act_sell"]}
    except Exception as e:
        log.exception("marker_ticks_symbol failed")
        return {"symbol": sym, "families": {}, "error": f"{type(e).__name__}: {str(e)[:200]}"}
