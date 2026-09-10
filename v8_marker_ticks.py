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

THIS RELEASE covers two families only, both genuinely zero-new-compute:
  CHAN  reads v8_channel_5m.compute_channels()'s in-memory `fits` dict — already computed for the
        FULL active-futures universe every 5-min tick. No re-evaluation, no second compute path;
        the caller (v8_channel_5m.run_tick) passes `fits` straight through after its own use.
  TCS   reads tc_universe_ticks (already all 207 symbols, all 4 buckets, cc#1862 /
        TC_CANON_V2_FINAL) and applies the SAME amber condition v8_pivot_star.run_tc_score_tick()
        already applies to the book (TC_STRONG_PCT / TC_TRAIL_DAYS, imported not retyped): the
        latest tick's score100 must exceed TC_STRONG_PCT AND exceed the 3-day trailing average of
        that bucket's own daily-last-tick series. One extra read per bucket for the trailing
        average — never a second scorer, never a re-run of Trade Check.

STARS, DMA (card item 5) and ACT (card item 6) are DEFERRED — not silently dropped. See the
STOPPED note logged on cc#1978 (cc_task_logs task_id=1199) and the FINDING posted there:
  - v8_pivot_star.evaluate() (stars) is scoped to the OPEN BOOK by EVAL_SCOPE="positions", which
    session_log 18052 FOUNDER-LOCKED for this exact marker ("18052 locks 'positions'" — the
    module's own docstring, v8_pivot_star.py:141-144). Flipping that global, as the card's item 5
    literally describes, would change the live book-scoped marker's behaviour for every existing
    consumer and would override a locked founder rule without a new explicit instruction naming
    that override (CLAUDE.md rule 12's supersession bar: permission must be explicit, for that
    specific rule, logged before it is acted on). v8_pivot_star.evaluate_dma_state() does not even
    read EVAL_SCOPE — its candidate query is a separate hardcoded open-book SELECT — so "flip one
    line" does not describe it as the code stands today. Neither function is touched here. A
    universe-scoped writer for STARS/DMA needs new, separately-scoped evaluation logic (reusing
    the same condition math via a shared helper, not a flag flip and not a duplicated copy) —
    proposed on the FINDING, not built, pending the founder's word.
  - ACT (v8_pivot_star.evaluate_activity) loops r6_read/_ad_21d per symbol; the cc#1977 census
    measured that at roughly 50-60 seconds for 208 symbols — unusable on a 5-minute beat. Batching
    those two reads is out of this module's zero-new-compute budget and is not attempted here.

Retention: 30 days rolling, purged on the day's LAST tick each family runs on — same "ship the
purge with the writer" convention as tc_universe_ticks.py / v8_channel_5m.py.
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
    chan_buy, tcs_buy_mom, tcs_buy_rev, tcs_sell_mom, tcs_sell_rev) — never a fabricated state.
    STARS/DMA/ACT families are not written yet (cc#1978, deferred — see this module's docstring)
    so they will not appear here until that follow-up ships."""
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
                "families_covered": sorted(_BUCKET_FAMILY.values()) + ["chan_sell", "chan_buy"]}
    except Exception as e:
        log.exception("marker_ticks_symbol failed")
        return {"symbol": sym, "families": {}, "error": f"{type(e).__name__}: {str(e)[:200]}"}
