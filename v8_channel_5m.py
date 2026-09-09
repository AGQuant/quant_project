"""
v8_channel_5m.py — cc#1880 GREEN_TRIANGLE_V1 (founder ask 09-Sep-2026, session_log via
cc_task_logs task_id=1199 ids 5790/5808/5820/5821).

A server-side 5-MINUTE regression channel over the last 3 trading days, per active-futures
symbol, plus the GREEN TRIANGLE rejection marker built on top of it. New small file per the
card's own instruction ("do not bloat v8_pivot_star.py") — this module owns the channel math
and the marker's write; v8_pivot_star.pivot_star() only gains a fifth READ list, matching the
established stars/activity/dma/tc_strong pattern exactly (cc#1008 DISPLAY_PARITY: markers are
read from the LOG for display, never live re-evaluated, so a fading intraday condition cannot
un-paint a marker the log already recorded).

THE RULE CHANGED TWICE DURING THIS CARD — READ THIS, NOT THE ORIGINAL SPEC TEXT.
The founder's original wording ("last 4 session") described DAILY bars. Founder correction,
09-Sep 16:22 IST (log 5820): "Check five minute session for last three days" — the window is
5-MINUTE bars, THREE TRADING DAYS, not 60 daily bars. session_log 5820/5821 (log ids in
cc_task_logs task_id=1199) are the founder-locked final rule and are what this file implements.
The daily-window table this card first proposed (v8_daily_channel) was DROPPED — it held zero
rows — and replaced by v8_channel_5m before a single write happened; do not resurrect either the
daily table name or the "4 sessions" wording anywhere, including a comment.

FINAL RULE (founder-locked, log 5821):
  WINDOW   5-min bars, last 3 TRADING days (today included, whatever bars exist so far), per
           symbol, source IN ('fyers_fut','fyers_fut_rest') (the same fut-first basis every
           other SmartGain/TC surface already uses — index_heal.py's FUT_SOURCES doctrine).
  CHANNEL  least-squares regression of close vs bar index over that window. UPPER = regression
           + max(high - regression) across the window; LOWER = regression + min(low - regression).
           Parallel lines by construction: every high <= upper and every low >= lower, always.
  SELL     some bar HIGH in the window came within 20 bps of the upper band
           (high >= upper_at_that_bar * UPPER_TOUCH_TOL), AND the latest close is >= 1% BELOW
           that bar's own CLOSE.
  BUY      mirror at the lower band: low <= lower_at_that_bar * LOWER_TOUCH_TOL, latest close
           >= 1% ABOVE that bar's close.
  Multiple qualifying bars -> the MOST RECENT one (log 5821: "the rule is about the latest
  rejection, not the first").

TRAP, stated because it bit the first cut of this rule: bar count per day is NOT constant
(circuit halts, partial sessions, feed gaps) — 07-Sep carried 65 bars against 08-Sep's 77 in the
founder's own 09-Sep 16:20 IST spot check. Never assume a fixed bar count; n_bars is recorded on
every write so a thin window is visible, never silently padded.

SCOPE SPLIT, deliberate: the CHANNEL is computed for the full ACTIVE FUTURES UNIVERSE (registry-
derived, futures_universe.is_active — card verify V2) and written for all of them every tick, but
the TRIANGLE MARKER only fires and logs for symbols on the OPEN PAPER BOOK (same "positions"
candidate scope evaluate()/evaluate_dma_state() already use in v8_pivot_star.py) — because a
marker only means something on a row the dashboard actually shows (card scope item 5: "in the
SYMBOL column of the four basket tabs"). SELL fires only on a SELL-basket row; BUY only on a
BUY-basket row — the founder's own basket/side pairing (card items 2-3), reusing `_direction()`
from v8_pivot_star.py rather than a second copy of that basket-prefix rule.

DDL: v8_channel_5m was created by Fable on founder approval (log 5808/5820), NOT by this module.
ensure_schema()'s CREATE TABLE IF NOT EXISTS mirrors that DDL exactly and is a no-op against the
live table — MAINTENANCE_LOCK_RULE cc#351 governs, same convention as v8_pivot_star.ensure_schema:
CREATE TABLE IF NOT EXISTS only, never an ALTER, from this codepath.

RETENTION: this writes ~207 symbols every 5 minutes, the same accumulation shape as
tc_universe_ticks (cc#1862) — TC_CANON_V2_FINAL's "ship the purge WITH the writer, not after" is
followed here too. Only 3 trading days are ever read, so PURGE_RETENTION_DAYS keeps a small buffer
past that (a fourth/fifth day) rather than trimming exactly to the read window, so a slow purge
tick or a late-running window resolution never reads past its own retention edge. Purge runs once
per day, on the LAST cash-continuous tick (15:20 IST, the same boundary _bg_pivot_star's own
caller uses) — same "ship on the last tick" convention as tc_universe_ticks.run_tick().
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import pytz

from v8_book_canon import retired_baskets   # cc#970 registry, same import v8_pivot_star.py uses
from v8_pivot_star import _direction        # cc#932's basket-prefix -> BUY/SELL rule, reused not copied

log = logging.getLogger("scorr.v8_channel_5m")
_DB = os.getenv("DATABASE_URL", "")
IST = pytz.timezone("Asia/Kolkata")

CHANNEL_WINDOW_TRADING_DAYS = 3    # founder-locked, log 5820/5821 — supersedes the original "60 daily bars"
UPPER_TOUCH_TOL = 0.998            # high >= upper * 0.998 — 20 bps tolerance, founder default (log 5790/5821)
LOWER_TOUCH_TOL = 1.002            # low  <= lower * 1.002 — mirror, same 20 bps
REVERSAL_PCT = 1.0                 # the 1% reversal-from-touch-close threshold, both sides
FUT_SOURCES = ("fyers_fut", "fyers_fut_rest")   # index_heal.py's FUT_SOURCES doctrine, reused
PURGE_RETENTION_DAYS = 6           # buffer past the 3-day read window (log 5820's "propose a purge")
MIN_BARS_TO_FIT = 2                # fewer points than this cannot define a line at all


def _conn():
    return psycopg2.connect(_DB)


def _ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def ensure_schema(conn):
    """CREATE TABLE IF NOT EXISTS only — mirrors the DDL Fable already applied (log 5820) so a
    fresh/other environment stays correct without a second hand-typed copy of the schema drifting
    from the live one. No ALTER TABLE anywhere in this module (MAINTENANCE_LOCK_RULE cc#351)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS v8_channel_5m (
              symbol varchar(32) NOT NULL,
              ts timestamptz NOT NULL,
              upper_band numeric NOT NULL,
              lower_band numeric NOT NULL,
              slope numeric,
              n_bars integer NOT NULL,
              window_start timestamptz NOT NULL,
              computed_at timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (symbol, ts)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_channel_5m_ts ON v8_channel_5m(ts DESC)")
    conn.commit()


def _window_start(cur) -> Optional[datetime]:
    """The start of the last CHANNEL_WINDOW_TRADING_DAYS trading days, universe-wide — the
    calendar dates that actually have fut 5m bars, not a fixed lookback interval (a long weekend
    or holiday would otherwise pull in a stale 4th day). Returns the EARLIEST date's own midnight,
    or None if there is no fut intraday history at all yet (a genuinely valid empty case)."""
    cur.execute("""
        SELECT MIN(d) FROM (
            SELECT DISTINCT ts::date AS d FROM intraday_prices
            WHERE source IN %s AND timeframe = '5m' AND ts::date <= CURRENT_DATE
            ORDER BY d DESC LIMIT %s
        ) recent_days""", (FUT_SOURCES, CHANNEL_WINDOW_TRADING_DAYS))
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def _fetch_bars(cur, symbols: List[str], window_start) -> Dict[str, List[Tuple]]:
    """ALL 5m fut bars for `symbols` from `window_start` onward, ONE query for the whole universe.
    DISTINCT ON (symbol, ts) with native fyers_fut preferred over the fyers_fut_rest fallback when
    both exist for the same bar (index_heal.py's FUT_SOURCES doctrine: both are valid, native
    wins on overlap)."""
    cur.execute("""
        SELECT DISTINCT ON (symbol, ts) symbol, ts, open, high, low, close
        FROM intraday_prices
        WHERE symbol = ANY(%s) AND timeframe = '5m' AND source IN %s AND ts >= %s
        ORDER BY symbol, ts, (source = 'fyers_fut') DESC
    """, (symbols, FUT_SOURCES, window_start))
    out: Dict[str, List[Tuple]] = {}
    for sym, ts, o, h, l, c in cur.fetchall():
        out.setdefault(sym, []).append((ts, float(o), float(h), float(l), float(c)))
    return out


def _fit_channel(bars: List[Tuple]) -> Optional[Dict[str, Any]]:
    """Pure-Python least-squares fit (n is small — a few hundred bars at most, no numpy needed).
    `bars` must already be ts-ascending. Returns the band + the MOST RECENT touch on each side, or
    None if there are too few bars to fit a line at all (MIN_BARS_TO_FIT)."""
    n = len(bars)
    if n < MIN_BARS_TO_FIT:
        return None
    closes = [b[4] for b in bars]
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(closes) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:   # every bar at the same index — cannot happen with n>=2 distinct rows, guarded anyway
        return None
    slope = sum((xs[i] - mean_x) * (closes[i] - mean_y) for i in range(n)) / denom
    intercept = mean_y - slope * mean_x

    def reg(i):
        return intercept + slope * i

    offset_up = max(bars[i][2] - reg(i) for i in range(n))     # max(high - regression)
    offset_down = min(bars[i][3] - reg(i) for i in range(n))   # min(low - regression), <= 0

    # Most-recent touch on each side. A later i always wins on a tie, matching the founder's
    # "most recent, not first" ruling (log 5821) — iterating i ascending and simply overwriting
    # on every qualifying bar gives that for free.
    sell_touch = None   # (ts, touch_close, bar_index)
    buy_touch = None
    for i in range(n):
        upper_i = reg(i) + offset_up
        lower_i = reg(i) + offset_down
        if bars[i][2] >= upper_i * UPPER_TOUCH_TOL:
            sell_touch = (bars[i][0], bars[i][4], i)
        if bars[i][3] <= lower_i * LOWER_TOUCH_TOL:
            buy_touch = (bars[i][0], bars[i][4], i)

    latest_ts, latest_close = bars[-1][0], bars[-1][4]
    upper_today = reg(n - 1) + offset_up
    lower_today = reg(n - 1) + offset_down

    result = {
        "n_bars": n, "slope": slope, "upper_today": upper_today, "lower_today": lower_today,
        "latest_ts": latest_ts, "latest_close": latest_close,
        "sell_trigger": None, "buy_trigger": None,
    }
    if sell_touch is not None:
        touch_ts, touch_close, _ = sell_touch
        pct_move = (latest_close - touch_close) / touch_close * 100.0
        if pct_move <= -REVERSAL_PCT:
            result["sell_trigger"] = {"touch_ts": touch_ts, "touch_close": touch_close, "pct_move": pct_move}
    if buy_touch is not None:
        touch_ts, touch_close, _ = buy_touch
        pct_move = (latest_close - touch_close) / touch_close * 100.0
        if pct_move >= REVERSAL_PCT:
            result["buy_trigger"] = {"touch_ts": touch_ts, "touch_close": touch_close, "pct_move": pct_move}
    return result


def compute_channels(conn, symbols: List[str]) -> Dict[str, Any]:
    """Full-universe sweep: fit + write ONE v8_channel_5m row per symbol this tick, and return the
    per-symbol fit results (kept in memory, not re-read) for evaluate_triangle_markers() below to
    scope down to the open book. PURE compute + write of v8_channel_5m only — never touches
    v8_pivot_star_log (that is evaluate_triangle_markers()'s job, kept separate so a channel-only
    caller/test never has a marker side-effect)."""
    with conn.cursor() as cur:
        window_start = _window_start(cur)
        if window_start is None or not symbols:
            return {"window_start": None, "fits": {}, "written": 0}
        bars_by_symbol = _fetch_bars(cur, symbols, window_start)

    fits: Dict[str, Any] = {}
    rows = []
    for sym in symbols:
        fit = _fit_channel(bars_by_symbol.get(sym, []))
        if fit is None:
            continue
        fits[sym] = fit
        rows.append((sym, fit["latest_ts"], round(fit["upper_today"], 4), round(fit["lower_today"], 4),
                      round(fit["slope"], 6), fit["n_bars"], window_start))

    written = 0
    if rows:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT INTO v8_channel_5m (symbol, ts, upper_band, lower_band, slope, n_bars, window_start)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, ts) DO NOTHING
                """, r)
                written += cur.rowcount
        conn.commit()
    return {"window_start": str(window_start), "fits": fits, "written": written, "universe": len(symbols)}


def evaluate_triangle_markers(conn, fits: Dict[str, Any]) -> Dict[str, Any]:
    """Scope `fits` (universe-wide) down to the OPEN paper book and log first-fire GREEN TRIANGLE
    markers into v8_pivot_star_log — same table, same ON CONFLICT DO NOTHING first-fire-only
    convention as BUY/SELL/ACTIVITY/DMA_ABOVE/DMA_BELOW/TC_STRONG (cc#933's column-reuse doctrine:
    no ALTER TABLE, level_value/pp/cmp_at_star/day_1d/touched_dates carry this marker's own facts
    under their existing names, documented here rather than renamed).

    COLUMN REUSE, STATED: level_value = signed pct move from touch close to latest close; pp =
    the touching bar's OWN close (not a pivot point — same reuse precedent as 5DMA_X_20DMA's pp);
    cmp_at_star = latest close; day_1d = n_bars (the window's bar count, so a thin-window fire is
    visible on the row without a second query); touched_dates = the touching bar's own date."""
    d = _ist_now().date()
    ts = _ist_now()
    with conn.cursor() as cur:
        _retired, _ = retired_baskets(cur)
        cur.execute("""
            SELECT DISTINCT ON (p.symbol) p.symbol, p.basket
            FROM v8_paper_positions p
            LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
            WHERE p.status = 'OPEN'
              AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
              AND NOT (p.basket = ANY(%(retired)s))
            ORDER BY p.symbol, p.entry_ts DESC""", {"retired": _retired})
        cands = [(r[0], r[1]) for r in cur.fetchall()]

    sell_fires, buy_fires = 0, 0
    with conn.cursor() as cur:
        for sym, basket in cands:
            fit = fits.get(sym)
            if not fit:
                continue
            side = _direction(basket)   # 'BUY' / 'SELL' / None, from the basket's own name prefix
            if side == "SELL" and fit.get("sell_trigger"):
                trg = fit["sell_trigger"]
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, basket, direction, star_color,
                       level_name, level_value, pp, cmp_at_star, day_1d, touched_dates)
                    VALUES (%s,%s,%s,%s,'CHAN_SELL','GREEN','CHAN_REJECT',%s,%s,%s,%s,%s::date[])
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (d, ts, sym, basket, round(trg["pct_move"], 2), round(trg["touch_close"], 2),
                      round(fit["latest_close"], 2), fit["n_bars"], [trg["touch_ts"].date()]))
                sell_fires += cur.rowcount
            if side == "BUY" and fit.get("buy_trigger"):
                trg = fit["buy_trigger"]
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, basket, direction, star_color,
                       level_name, level_value, pp, cmp_at_star, day_1d, touched_dates)
                    VALUES (%s,%s,%s,%s,'CHAN_BUY','GREEN','CHAN_REJECT',%s,%s,%s,%s,%s::date[])
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (d, ts, sym, basket, round(trg["pct_move"], 2), round(trg["touch_close"], 2),
                      round(fit["latest_close"], 2), fit["n_bars"], [trg["touch_ts"].date()]))
                buy_fires += cur.rowcount
    conn.commit()
    return {"candidates": len(cands), "sell_new": sell_fires, "buy_new": buy_fires}


def _purge_old(conn) -> Optional[int]:
    """DELETE rows older than PURGE_RETENTION_DAYS. Called once per day, on the last tick — same
    "ship the purge WITH the writer" convention as tc_universe_ticks.run_tick()."""
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM v8_channel_5m WHERE ts < NOW() - INTERVAL '%s days'" % PURGE_RETENTION_DAYS)
            purged = cur.rowcount
        conn.commit()
        return purged
    except Exception as e:
        log.warning("v8_channel_5m purge failed (non-fatal, next tick retries): %s", e)
        return None


def run_tick(conn=None) -> Dict[str, Any]:
    """One 5-min tick: ensure schema, sweep the full active-futures universe (compute + write
    channels), scope down to the open book and log first-fire GREEN TRIANGLE markers, purge on
    the day's last tick. Registry-derived universe (futures_universe.is_active) — never a
    hardcoded symbol list (ENGINE_LIVENESS_RULE 13829)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active")
            universe = [r[0] for r in cur.fetchall()]
        chan = compute_channels(conn, universe)
        if not chan["fits"]:
            return {"ok": True, "universe": len(universe), "window_start": chan["window_start"],
                     "computed": 0, "sell_new": 0, "buy_new": 0, "purged": None,
                     "zero_tick": True, "note": "no fut 5m bars in window yet — valid empty tick"}
        markers = evaluate_triangle_markers(conn, chan["fits"])
        purged = None
        now = _ist_now()
        if now.hour == 15 and now.minute == 20:
            purged = _purge_old(conn)
        return {"ok": True, "universe": len(universe), "window_start": chan["window_start"],
                "computed": len(chan["fits"]), "candidates": markers["candidates"],
                "sell_new": markers["sell_new"], "buy_new": markers["buy_new"], "purged": purged,
                "zero_tick": markers["sell_new"] == 0 and markers["buy_new"] == 0}
    except Exception as e:
        log.exception("v8_channel_5m tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
