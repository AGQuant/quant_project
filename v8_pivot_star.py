"""
v8_pivot_star.py — cc#856 PIVOT_STAR_V1 (founder 05-Aug-2026).

A read-side REVERSAL MARKER on V8 signals, plus a measurement log.

  BLUE star  a BUY-basket signal that touched S1 in the last 3 CLOSED sessions, is now within
             2% of today's S1, is up on the day, and is above its 50-DMA.
  RED star   the mirror on R1 for SELL-basket signals.

WHY THIS IS A MARKER AND NOT A BASKET — THE WHOLE POINT OF THE CARD.
session_log 5646 (BUY_S1_BOUNCE_KILLED_17JUL) killed buy_s1_bounce on 17-Jul-2026. A 1-year
5-minute replay of this exact condition set produced 9 trades / 55.6% WR / +0.07 EV, and with the
Nifty gate removed 53 trades / 34.0% WR / -0.75 EV. As a TRADE RULE this is known-negative. It is
permitted here only as CONTEXT on a signal that already qualified through a basket that does have
positive evidence. So:

  * nothing in this module writes to v8_qualified, v8_paper_* or any slot,
  * no star can create a paper entry,
  * no UI copy may imply an entry (see the tooltip contract in `star_note`),
  * v8_signal_writer.py is never imported or touched.

EVALUATION SCOPE — AND A CONTRADICTION IN THE CARD, RESOLVED DELIBERATELY.
The card's scope items 3 and 4 say the rule is "evaluated only on symbols already present in
v8_qualified" for a BUY / SELL basket, and item 10 says the glyph renders "next to the symbol on
the V8 signal rows" — you can only mark a signal row if the symbol IS one. So the star is
qualified-scoped, and that is what this module does.

But the card's own verify items expect 7 red stars on 04-Aug, and its evidence block cites a
"universe: 209 active futures" study. Those cannot both hold: on 04-Aug v8_qualified carried
exactly ONE sell-basket row (sell_momentum), so the maximum possible red count under the stated
scope is 1, not 7. Reproducing the founder's funnel over the FUTURES UNIVERSE instead returns
blue 63 / red 97 over 22 sessions with 8 red on 04-Aug — the same shape as their 50 / 80 / 7, so
their study was clearly universe-wide. Read together, the universe numbers are a FEASIBILITY test
of the rule (it fires ~2-4 times a day across 209 symbols, so it will not flood anything), not a
prediction of how many stars appear on screen. The stars themselves are a subset landing on
qualified signals.

EVAL_SCOPE below makes that switchable in one line if the founder wants the universe reading.

The one founder finding this module DOES reproduce exactly: red stars with cmp < dma_50 return
ZERO rows over 22 sessions. Condition (d) is deliberately NOT inverted for the red star — a stock
at R1 is at recent highs by construction. Do not "fix" it.
"""

import os
import logging
from datetime import date, datetime
from typing import Dict, Any, List, Optional

import psycopg2
import pytz
from fastapi import APIRouter
from v8_book_canon import retired_baskets   # cc#970: retired-basket registry (rule 13)

log = logging.getLogger("scorr.pivot_star")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
IST = pytz.timezone("Asia/Kolkata")

TOUCH_SESSIONS = 3      # CLOSED sessions only — never an intraday low/high (card item 6)
NEAR_LEVEL_PCT = 2.0    # band width either side of the level, in %
NEAR_PP_PCT    = 1.0    # recorded only, never rendered (card item 5)

# ── cc#932 PIVOT_STAR_V2 — founder-locked in session_log 18052 ────────────────────────────────
# The V1 conditions and scope are SUPERSEDED. V2, verbatim from 18052:
#   BUY  (blue)
#     1 TOUCH     in the last 3 closed sessions, session low <= THAT session's own S1
#     2 POSITION  cmp between S1 and S1*1.02  OR  cmp > PP
#     3 STABILITY day_1d between -1 and +2  AND  mom_2d between -1 and +2
#   SELL (red) — mirrored on R1
#     1 TOUCH     session high >= that session's own R1
#     2 POSITION  cmp between R1*0.98 and R1  OR  cmp < PP
#     3 STABILITY day_1d between -2 and +1  AND  mom_2d between -2 and +1
# V1's "up on the day" and "above 50-DMA" clauses are GONE — the stability band replaces them.
# dma_50 is still read and still logged, because the column exists and dropping the record would
# lose history for the 4-6 week review; it is simply no longer a condition.
STAB_BUY  = (-1.0, 2.0)     # day_1d and mom_2d must BOTH sit inside this, buy side
STAB_SELL = (-2.0, 1.0)     # mirrored band, sell side

# GLYPH IS PART OF THE SPEC, not a template choice. Served from here so web and mobile cannot
# disagree about it (DISPLAY_PARITY 16202).
#
# cc#1018 MARKER_GLYPH_V3 (founder-locked, session_log 21764): BOTH SIDES DRAW A STAR. This
# SUPERSEDES the glyph clause of 18052 ("sell = circle, never a star on the sell side"). The
# CONDITIONS above are untouched — only the shape drawn changes. Colour still carries the side
# (blue = S1 reversal, red = R1 mirror), so the circle was doing no work the colour was not
# already doing. The only marker whose SHAPE still varies by side is the GREEN activity one below.
GLYPH = {"BUY": "star", "SELL": "star"}

# ── cc#933 GREEN_STAR_ACTIVITY_V1 — founder-locked in session_log 18053 ───────────────────────
# cc#1441 push 4 (VOLUME_METRICS_CANON_V2, session_log 33843): the volume side moves off
# v8_metrics.vol_ratio (which was a 10-DAY-average day ratio — the old "(a) 21-day average"
# comment here was stale, verified 30-Aug) onto the canon pair. Same open-positions scope as V2.
# Fires when ANY leg trips:
#   (a) RVOL  > ACTIVITY_RVOL_X  — today's slot-normalized pace (rvol_engine, profile read)
#   (b) VOL P > ACTIVITY_VOLP_Y  — the prior session's closing RVOL (same formula, prior day)
#   (c) |OI day-over-day| > 25%  — futures_basis, last tick of the day vs last tick of the prior
#       session. UNTOUCHED. 25% is deliberately RARE: typical DoD is 1-5%, so this leg fires only
#       on a true event or a rollover, and it is expected to be silent most days.
#
# READ 18053 CAREFULLY — two of its keys look contradictory and are not. `founder_amendment_08aug`
# says there is NO side split; `founder_final_08aug` says BUY shows a star and SELL a circle. They
# reconcile cleanly: the CONDITION has no side split (identical test both sides, no mirrored band),
# while the GLYPH does. One condition, one meaning — unusual activity — drawn in the shape of the
# side it sits on.
#
# AND THE SIDE HERE IS THE POSITION'S OWN SIDE — the opposite of cc#932. That is deliberate, not an
# inconsistency: a pivot marker describes how the STOCK is behaving against its own levels (so
# MAXHEALTH can carry a SHORT position and a BUY star), whereas activity has no directional reading
# at all — volume and OI say "something is happening", not "up" or "down". So the only side it can
# honestly take is the side you are on. DO NOT unify these two side rules later; they answer
# different questions.
# cc#1441: 1.5 is the like-for-like carry-over of the retired vol_ratio>1.5 bar (closing-RVOL
# >= 1.5 selects ~15% of symbol-days, comparable selectivity — backtest + before/after fire
# rates in the task log: open book 4->5 of 21, universe 12.71%->19.95%). Shipped values stood
# through the 30-Aug founder sign-off round (cc_task_logs, task 1441) — FINAL with the V13/R6
# batch; change only via a new sign-off.
ACTIVITY_RVOL_X      = 1.5   # FINAL — 30-Aug-2026 sign-off round (cc#1441)
ACTIVITY_VOLP_Y      = 1.5   # FINAL — 30-Aug-2026 sign-off round (cc#1441)
ACTIVITY_OI_DOD_PCT  = 25.0  # untouched (18053)

# cc#1024 MARKER_GLYPH_V5 (founder-locked, session_log 22296): the activity marker is a LIGHTNING
# BOLT, U+26A1, on both sides. This retires BOTH earlier forms — the circle-for-short of 18053 and
# the green star of V4 — and with them the last shape-by-side rule on the board.
#
# The reasoning holds together with cc#1018: once the pivot marker stopped changing shape by side,
# a lone green marker still doing it was the only thing keeping "shape = side" alive in a reader's
# head, for the ONE marker whose meaning has no direction at all. Volume and OI say "something is
# happening here", never "up" or "down". So the family is now: STAR = a level or a check was met,
# colour says which; BOLT = a spurt. Shape carries meaning, never side.
#
# The bolt is drawn with NO colour styling — it is an emoji and renders in its own. The value below
# is still keyed by side so the payload shape and every existing consumer keep working; both keys
# simply answer the same thing now, exactly as GLYPH did after cc#1018.
GLYPH_SIDE = {"LONG": "bolt", "SHORT": "bolt"}

# cc#932: the scope is now the OPEN PAPER BOOK. "qualified" (V1) and "universe" (the founder's
# feasibility read) are kept so either is a one-line switch, but 18052 locks "positions".
# Changing this changes WHICH SYMBOLS ARE EVALUATED only — never what the rule is.
EVAL_SCOPE = "positions"

# RETIRED BY cc#932, KEPT DELIBERATELY. Under V1 the basket prefix decided which side a symbol was
# tested on. V2 tests BOTH sides on every candidate — the marker describes the STOCK's behaviour
# against its own pivots, not the direction we are positioned in (18052's own example, MAXHEALTH,
# is a SHORT position carrying a BUY star). Left in place because the "universe"/"qualified" scopes
# still exist as one-line switches and would want it back; it has no caller today.
def _direction(basket: str) -> Optional[str]:
    b = (basket or "").lower()
    if b.startswith("buy"):
        return "BUY"
    if b.startswith("sell"):
        return "SELL"
    return None


def _conn():
    return psycopg2.connect(_DB)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def ensure_schema(conn):
    """CREATE TABLE only. There is deliberately NO ALTER TABLE anywhere in this module —
    MAINTENANCE_LOCK_RULE (cc#351) confines those to a weekend Railway console window, and cc#857
    is concurrently removing per-request DDL from the R card for the same reason."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS v8_pivot_star_log (
              id bigserial PRIMARY KEY,
              star_date date NOT NULL,
              first_seen_ts timestamp NOT NULL,
              symbol text NOT NULL,
              basket text,
              direction text NOT NULL,
              star_color text NOT NULL,
              level_name text,
              level_value numeric,
              pp numeric,
              cmp_at_star numeric,
              pct_from_level numeric,
              near_pp boolean,
              day_1d numeric,
              dma_50 numeric,
              touched_dates date[],
              created_at timestamp DEFAULT (NOW() AT TIME ZONE 'Asia/Kolkata'),
              CONSTRAINT v8_pivot_star_log_uniq UNIQUE (symbol, star_date, direction)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pivot_star_date ON v8_pivot_star_log(star_date DESC)")
        # cc#1540 (founder cadence amendment, cc_task_logs 4292): the Trade Check TICK SERIES —
        # every 5 minutes during market hours, NOT once daily as the card first said. Multiple
        # ticks per day are expected and wanted, so the key is (symbol, ts, side) with no daily
        # uniqueness. The amber marker's 3-day trailing average DERIVES a daily series from this
        # (each day's representative = that day's LAST tick — stated choice, applied
        # consistently). 30-day rolling retention runs inside the same job.
        # CREATE TABLE only, same MAINTENANCE_LOCK_RULE governance as the log above.
        # (v8_tc_score_daily, the amendment-superseded daily table, exists empty in the live DB
        # from the first cut of this card — flagged for a weekend console DROP, never written.)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS v8_tc_score_ticks (
              symbol TEXT NOT NULL,
              ts TIMESTAMPTZ NOT NULL,
              side TEXT NOT NULL,
              score NUMERIC,
              total NUMERIC,
              score_pct NUMERIC,
              verdict_class TEXT,
              PRIMARY KEY (symbol, ts, side)
            )""")
        # cc#1541 P0: the original index here was ON ((ts::date), symbol) — ts is timestamptz and
        # ts::date is timezone-dependent (STABLE, not IMMUTABLE), so Postgres rejects it as an
        # index expression and the CREATE INDEX raised on EVERY tick, killing the whole job at
        # this line before a single row was ever written. Plain immutable columns only; do NOT
        # reintroduce any expression index here (AT TIME ZONE with a named zone is equally
        # STABLE). The day-grouping reads stay correct without it — at 30-day retention and one
        # open book of symbols this table is small.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tc_score_ticks_symbol_ts ON v8_tc_score_ticks(symbol, ts DESC)")
    conn.commit()


# ── evaluation ────────────────────────────────────────────────────────────────────────────────
def evaluate(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """Return today's stars. PURE READ — this function writes nothing."""
    d = target_date or _ist_now().date()
    with conn.cursor() as cur:
        # Candidate set. Under the card's scope this is v8_qualified for today; the universe branch
        # exists only so the founder's feasibility reading is reproducible without a code rewrite.
        if EVAL_SCOPE == "universe":
            cur.execute("""SELECT symbol, NULL::text AS basket FROM futures_universe WHERE is_active""")
        elif EVAL_SCOPE == "positions":
            # cc#932: the OPEN paper book. Same era scope the book itself uses everywhere else
            # (cc#504 cutover, retired baskets excluded via the cc#970 registry), so this marks the ones
            # is actually looking at and cannot mark a row the book does not show.
            _retired, _ = retired_baskets(cur)   # resolved BEFORE the main query: same cursor
            cur.execute("""
                SELECT DISTINCT ON (p.symbol) p.symbol, p.basket
                FROM v8_paper_positions p
                LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
                WHERE p.status = 'OPEN'
                  AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
                  AND NOT (p.basket = ANY(%(retired)s))
                ORDER BY p.symbol, p.entry_ts DESC""", {"retired": _retired})
        else:
            cur.execute("""SELECT DISTINCT ON (symbol) symbol, basket
                           FROM v8_qualified WHERE signal_date=%s
                           ORDER BY symbol, id DESC""", (d,))
        cands = [(r[0], r[1]) for r in cur.fetchall()]
        if not cands:
            return []
        syms = [c[0] for c in cands]

        # Today's pivots.
        cur.execute("""SELECT symbol, s1, r1, pp FROM v8_paper_pivots
                       WHERE pivot_date=%s AND symbol = ANY(%s)""", (d, syms))
        piv = {r[0]: (_f(r[1]), _f(r[2]), _f(r[3])) for r in cur.fetchall()}

        # Live CMP through the SHARED resolver (cc#811/#835) — never a private price path. Falls
        # back to the last close so an out-of-hours run still evaluates rather than returning empty.
        live = {}
        try:
            import cmp_resolver
            live = cmp_resolver.resolve_cmp_many(cur, syms)
        except Exception as e:
            log.warning("cc#856 live CMP unavailable, using last close: %s", e)
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                       WHERE symbol = ANY(%s) AND close > 0 ORDER BY symbol, price_date DESC""", (syms,))
        eod = {r[0]: _f(r[1]) for r in cur.fetchall()}

        # cc#932: mom_2d joins day_1d — both are needed for the V2 stability band.
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, dma_50, day_1d, mom_2d FROM v8_metrics
                       WHERE symbol = ANY(%s) AND score_date <= %s
                       ORDER BY symbol, score_date DESC""", (syms, d))
        met = {r[0]: (_f(r[1]), _f(r[2]), _f(r[3])) for r in cur.fetchall()}

        # THE TOUCH TEST — CLOSED SESSIONS ONLY (card item 6). Each prior session's low/high is
        # compared against THAT SESSION'S OWN pivot, never today's: a pivot is only meaningful for
        # the day it was computed for, and comparing an old low to a new S1 would invent touches.
        cur.execute("""
            WITH sess AS (
                SELECT DISTINCT price_date FROM raw_prices
                WHERE price_date < %s ORDER BY price_date DESC LIMIT %s)
            SELECT rp.symbol,
                   ARRAY_AGG(rp.price_date ORDER BY rp.price_date) FILTER (WHERE rp.low  <= pv.s1) AS s1_dates,
                   ARRAY_AGG(rp.price_date ORDER BY rp.price_date) FILTER (WHERE rp.high >= pv.r1) AS r1_dates
            FROM raw_prices rp
            JOIN sess ON sess.price_date = rp.price_date
            JOIN v8_paper_pivots pv ON pv.symbol = rp.symbol AND pv.pivot_date = rp.price_date
            WHERE rp.symbol = ANY(%s)
            GROUP BY rp.symbol
        """, (d, TOUCH_SESSIONS, syms))
        touch = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    def _band(v, lo_hi):
        return v is not None and lo_hi[0] <= v <= lo_hi[1]

    out = []
    for sym, basket in cands:
        p = piv.get(sym)
        m = met.get(sym)
        if not p or not m:
            continue
        s1, r1, pp = p
        dma_50, day_1d, mom_2d = m
        cmp_v = (live.get(sym) or {}).get("cmp") if live else None
        if cmp_v is None:
            cmp_v = eod.get(sym)
        # dma_50 is no longer a CONDITION (V2), so it must not gate evaluation either — it is only
        # carried into the log. day_1d and mom_2d ARE conditions, so a missing one skips the symbol
        # rather than being treated as passing.
        if cmp_v is None or day_1d is None or mom_2d is None:
            continue
        s1_dates, r1_dates = touch.get(sym, (None, None))

        # near_pp is COMPUTED AND STORED but never rendered and never part of the star condition
        # (card item 5). Founder-tested: a PP clause alone produced 331 stars over 22 sessions,
        # peaking at 69 of 209 in a day — it would flood the screen. Recorded for a 4-6 week review.
        near_pp = bool(pp and abs(cmp_v - pp) / pp * 100.0 <= NEAR_PP_PCT)

        # cc#932 V2. NOTE the direction is NOT taken from the position's own side: the marker
        # describes how the STOCK is behaving against its pivots, not which way we happen to be
        # positioned. Fable's locked example proves it — MAXHEALTH carries a SHORT position and is
        # the one BUY star on 07-Aug. Buy side is tested first, so a symbol can only take one glyph.
        star = None
        if s1 and s1_dates:
            pos_ok = (s1 <= cmp_v <= s1 * (1.0 + NEAR_LEVEL_PCT / 100.0)) or (pp is not None and cmp_v > pp)
            if pos_ok and _band(day_1d, STAB_BUY) and _band(mom_2d, STAB_BUY):
                star = ("BLUE", "S1", s1, s1_dates)
        if star is None and r1 and r1_dates:
            pos_ok = (r1 * (1.0 - NEAR_LEVEL_PCT / 100.0) <= cmp_v <= r1) or (pp is not None and cmp_v < pp)
            if pos_ok and _band(day_1d, STAB_SELL) and _band(mom_2d, STAB_SELL):
                star = ("RED", "R1", r1, r1_dates)
        if star is None:
            continue

        colour, level_name, level_value, tdates = star
        out.append({
            "symbol": sym, "basket": basket,
            "direction": "BUY" if colour == "BLUE" else "SELL",
            "star_color": colour, "level_name": level_name,
            "level_value": round(level_value, 2),
            "pp": round(pp, 2) if pp else None,
            "cmp_at_star": round(cmp_v, 2),
            "pct_from_level": round((cmp_v - level_value) / level_value * 100.0, 2),
            "near_pp": near_pp,
            "day_1d": round(day_1d, 2),
            "mom_2d": round(mom_2d, 2),
            "dma_50": round(dma_50, 2) if dma_50 is not None else None,
            "glyph": GLYPH["BUY" if colour == "BLUE" else "SELL"],
            "touched_dates": [str(x) for x in (tdates or [])],
        })
    return out


def evaluate_activity(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """GREEN activity markers on the OPEN book (cc#933 / session_log 18053). PURE READ.

    Kept as its own function and its own response list rather than folded into evaluate(), because
    a symbol can legitimately carry BOTH a pivot marker and an activity marker at once. Merging
    them into one keyed map would silently drop one of the two — the surfaces render them side by
    side."""
    d = target_date or _ist_now().date()
    with conn.cursor() as cur:
        _retired, _ = retired_baskets(cur)       # resolved BEFORE the main query: same cursor
        cur.execute("""
            SELECT DISTINCT ON (p.symbol) p.symbol, p.side
            FROM v8_paper_positions p
            LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
            WHERE p.status = 'OPEN'
              AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
              AND NOT (p.basket = ANY(%(retired)s))
            ORDER BY p.symbol, p.entry_ts DESC""", {"retired": _retired})
        pos = [(r[0], (r[1] or "").upper()) for r in cur.fetchall()]
        if not pos:
            return []
        syms = [x[0] for x in pos]

        # cc#1441 push 4 (canon V2): RVOL for the latest session <= d, VOL P = the session before —
        # both through rvol_engine's one derivation. Date-aware so a replayed tick for a past date
        # reads that date's ratios, exactly as the old score_date<=d read did.
        from rvol_engine import day_rvol_batch
        cur.execute("""SELECT DISTINCT ts::date AS sd FROM intraday_prices
                       WHERE source = 'fyers_eq' AND ts::date <= %s
                       ORDER BY sd DESC LIMIT 2""", (d,))
        _days = [r[0] for r in cur.fetchall()]
        rvl = day_rvol_batch(cur, syms, _days[0]) if _days else {}
        vpl = day_rvol_batch(cur, syms, _days[1]) if len(_days) > 1 else {}

        # OI day-over-day: LAST tick of each session, this session vs the one before it.
        cur.execute("""
            WITH t AS (
                SELECT DISTINCT ON (symbol, ts::date) symbol, ts::date AS d, oi
                FROM futures_basis
                WHERE symbol = ANY(%s) AND ts::date <= %s
                ORDER BY symbol, ts::date DESC, ts DESC),
            l AS (
                SELECT symbol, d, oi, LAG(oi) OVER (PARTITION BY symbol ORDER BY d) AS prev_oi,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn
                FROM t)
            SELECT symbol, CASE WHEN prev_oi > 0 THEN (oi - prev_oi) / prev_oi * 100.0 END
            FROM l WHERE rn = 1""", (syms, d))
        oidod = {r[0]: _f(r[1]) for r in cur.fetchall()}

    out = []
    for sym, side in pos:
        rv, vp, od = rvl.get(sym), vpl.get(sym), oidod.get(sym)
        rv_hit = rv is not None and rv > ACTIVITY_RVOL_X
        vp_hit = vp is not None and vp > ACTIVITY_VOLP_Y
        vol_hit = rv_hit or vp_hit
        oi_hit = od is not None and abs(od) > ACTIVITY_OI_DOD_PCT
        if not (vol_hit or oi_hit):
            continue
        facts = []
        if rv_hit:
            facts.append(f"RVOL {rv:.1f}x its usual pace")
        if vp_hit:
            facts.append(f"prev close RVOL {vp:.1f}x")
        if oi_hit:
            facts.append(f"OI {od:+.0f}% day-over-day")
        out.append({
            "symbol": sym, "side": side,
            "star_color": "GREEN",
            "glyph": GLYPH_SIDE.get(side, "star"),
            "rvol": round(rv, 2) if rv is not None else None,
            "vol_p": round(vp, 2) if vp is not None else None,
            "oi_dod_pct": round(od, 2) if od is not None else None,
            "trigger": "VOL" if vol_hit and not oi_hit else ("OI" if oi_hit and not vol_hit else "VOL+OI"),
            # FACTS ONLY, same wall as star_note(): no buy/sell/entry/target wording.
            "note": " · ".join(facts),
        })
    return out


def _ist_market_hours() -> bool:
    """cc#1539: NSE Mon-Fri 09:15-15:30 IST — same gate check_endpoint.py's fibcheck uses to
    exclude today's mid-session partial candle from close-based math."""
    now = _ist_now()
    mins = now.hour * 60 + now.minute
    return (now.weekday() < 5) and (555 <= mins <= 930)


# ── cc#1539 DMA_CROSS_V1 (founder direct 31-Aug) — the THIRD marker family ────────────────────
# A small GREEN/RED SQUARE on the same open-book cards as the star and bolt: the 5-day simple
# moving average crossing the 20-day. A CROSS, not a STATE — it fires only on the session the
# relationship flips, so a symbol sitting above its 20DMA for weeks does not relight daily.
# Both SMAs are computed FRESH from raw_prices closes: there is no dma_5 anywhere in the DB, and
# the existing dma_20/50/200 columns store PERCENT DISTANCE from the MA, not the MA price level
# (v13_presets_endpoints.py documents this) — so nothing stored is usable for a crossover test,
# and computing fresh also avoids MAINTENANCE_LOCK_RULE's ALTER TABLE gate entirely.
DMA_FAST, DMA_SLOW = 5, 20
DMA_FETCH = 25          # 5+20 with headroom for a short-history symbol


def evaluate_dma_cross(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """GREEN/RED square markers on the OPEN book (cc#1539). PURE READ.

    Same candidate query as evaluate()/evaluate_activity() — the founder's ask is explicitly
    'just like star and bolt', i.e. the same cards. While the market is open, today's raw_prices
    row (a mid-session partial candle) is EXCLUDED — a partial close would make the cross fire
    and un-fire intraday, which is not a real signal. Fewer than DMA_SLOW+1 completed closes
    (today's AND yesterday's 20DMA both need a full window) skips the symbol — insufficient
    history, never a guessed cross."""
    d = target_date or _ist_now().date()
    with conn.cursor() as cur:
        _retired, _ = retired_baskets(cur)   # resolved BEFORE the main query: same cursor
        cur.execute("""
            SELECT DISTINCT ON (p.symbol) p.symbol, p.basket
            FROM v8_paper_positions p
            LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
            WHERE p.status = 'OPEN'
              AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
              AND NOT (p.basket = ANY(%(retired)s))
            ORDER BY p.symbol, p.entry_ts DESC""", {"retired": _retired})
        cands = [(r[0], r[1]) for r in cur.fetchall()]
        if not cands:
            return []
        syms = [c[0] for c in cands]

        # Completed closes only: during market hours today's row is a partial candle and is
        # excluded (the fibcheck pattern); after the close today's row IS the completed candle.
        ceiling_op = "<" if _ist_market_hours() else "<="
        cur.execute(f"""
            SELECT symbol, close FROM (
                SELECT symbol, close,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                FROM raw_prices
                WHERE symbol = ANY(%s) AND close > 0 AND price_date {ceiling_op} %s
            ) x WHERE rn <= %s
            ORDER BY symbol, rn""", (syms, d, DMA_FETCH))
        closes: Dict[str, List[float]] = {}
        for sym, close in cur.fetchall():
            closes.setdefault(sym, []).append(float(close))

        live = {}
        try:
            import cmp_resolver
            live = cmp_resolver.resolve_cmp_many(cur, syms)
        except Exception as e:
            log.warning("cc#1539 live CMP unavailable, using last close: %s", e)

        cur.execute("""SELECT DISTINCT ON (symbol) symbol, day_1d FROM v8_metrics
                       WHERE symbol = ANY(%s) AND score_date <= %s
                       ORDER BY symbol, score_date DESC""", (syms, d))
        met = {r[0]: _f(r[1]) for r in cur.fetchall()}

    out = []
    for sym, basket in cands:
        c = closes.get(sym) or []          # newest first
        if len(c) < DMA_SLOW + 1:
            continue                        # insufficient history — never a guessed cross
        sma5_t = sum(c[0:DMA_FAST]) / DMA_FAST
        sma20_t = sum(c[0:DMA_SLOW]) / DMA_SLOW
        sma5_y = sum(c[1:DMA_FAST + 1]) / DMA_FAST
        sma20_y = sum(c[1:DMA_SLOW + 1]) / DMA_SLOW
        if sma5_t > sma20_t and sma5_y <= sma20_y:
            colour, direction, rel = "GREEN", "DMA_CROSS_UP", "above"
        elif sma5_t < sma20_t and sma5_y >= sma20_y:
            colour, direction, rel = "RED", "DMA_CROSS_DOWN", "below"
        else:
            continue                        # no FRESH cross this session — nothing to mark
        cmp_v = (live.get(sym) or {}).get("cmp") if live else None
        if cmp_v is None:
            cmp_v = c[0]                    # last completed close — honest fallback
        out.append({
            "symbol": sym, "basket": basket,
            "direction": direction, "star_color": colour,
            "level_name": "5DMA_X_20DMA",
            "level_value": round(sma5_t, 2),    # the 5DMA
            "pp": round(sma20_t, 2),            # the 20DMA — see the column-reuse note in run_tick
            "cmp_at_star": round(float(cmp_v), 2),
            "day_1d": met.get(sym),
            "glyph": "square",
            # FACTS ONLY, same wall as star_note(): no buy/sell/entry/target wording.
            "note": f"5DMA {sma5_t:,.2f} crossed {rel} 20DMA {sma20_t:,.2f}",
        })
    return out


# ── cc#1540 TC_STRONG_V1 (founder direct 31-Aug; cadence amended same day, log 4292) ──────────
# An AMBER star when Trade Check's score is above 80% AND rising against its own 3-day trailing
# average. Unlike the other three families this needs HISTORY: no persistence of a Trade Check
# score existed anywhere (checked — the tc_score_* tables belong to TC SCANNER's replay engine,
# a different "TC"), so run_tc_score_tick() builds a 5-MINUTE series in v8_tc_score_ticks (the
# founder's amended cadence — market hours, same 5-min beat as run_tick) and fires the marker
# off a DAILY series derived from it: each day's representative score_pct is that day's LAST
# tick. 30-day rolling retention keeps the tick table bounded.
#
# PERF, measured not assumed: compute_trade_check is ~15 DB queries per symbol; at the current
# open book (~12 positions) a tick costs ~180 lightweight reads. The job logs its own elapsed_ms
# every run so a growing book shows up in scheduler_master timings, not as a silent slow tick.
TC_STRONG_PCT = 80.0     # the current tick's score_pct must exceed this…
TC_TRAIL_DAYS = 3        # …and exceed the average of the 3 most recent PRIOR days' last ticks
TC_RETENTION_DAYS = 30   # rolling window on the tick table (founder amendment)


def run_tc_score_tick(conn=None) -> Dict[str, Any]:
    """One 5-min market-hours tick: score every open-book position with Trade Check, append to
    the tick series, apply 30-day retention, then fire AMBER (direction='TC_STRONG') into
    v8_pivot_star_log where the condition holds (first-fire-only per symbol per day).

    SIDE IS THE POSITION'S OWN SIDE — deliberately unlike the pivot star (which tests both sides
    regardless of position). Trade Check inherently asks "does this LONG/SHORT setup validate",
    so the only honest reading for a marker on a position's own card is that position's side.

    The 3-day gate is FORWARD-LOOKING and reads the DERIVED daily series (last tick per prior
    day): fewer than TC_TRAIL_DAYS prior days with ticks skips the amber evaluation entirely
    (never a partial average, never a backfilled one) — so the marker cannot fire for ANY symbol
    until 3+ trading days after this ships. That silence is correct, not a defect."""
    import time as _t
    t0 = _t.monotonic()
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        d = _ist_now().date()
        ts = _ist_now()
        with conn.cursor() as cur:
            _retired, _ = retired_baskets(cur)
            cur.execute("""
                SELECT DISTINCT ON (p.symbol) p.symbol, p.side
                FROM v8_paper_positions p
                LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
                WHERE p.status = 'OPEN'
                  AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
                  AND NOT (p.basket = ANY(%(retired)s))
                ORDER BY p.symbol, p.entry_ts DESC""", {"retired": _retired})
            pos = [(r[0], (r[1] or "LONG").upper()) for r in cur.fetchall()]

        # cc#1548 P0: native_trade_check is NOT the platform's primary Trade Check engine — it is
        # one of the older, non-primary scorers tc_resolver.py's own docstring names explicitly.
        # The real primary is the 4-bucket best-of-side engine behind tc_resolver.get_primary_tc()
        # (v4.0), reached here via get_primary_styles() (its style-resolving variant, cc#748) so a
        # position is scored on its OWN side's two style cards (BUY-MOM/BUY-REV or SELL-MOM/
        # SELL-REV) and best_card() (cc#1033, founder-locked) picks the winner by score/max ratio —
        # exactly the "position's own side, best-of" behaviour cc#1540 wanted, on the right engine.
        # cc#728/#738 already lock side-narrowing; this fix only repoints WHICH engine answers it.
        from tc_resolver import get_primary_styles
        scorer = get_primary_styles()
        scored, failed, wrote = 0, 0, 0
        for sym, side in pos:
            # v8_paper_positions speaks LONG/SHORT; tc_v4_dual speaks BUY/SELL — never assumed
            # interchangeable. `side` (LONG/SHORT) is still what gets STORED, unchanged, since
            # every downstream reader (tc_score_latest, the amber query below, cc#1547's popover)
            # joins against the position's own LONG/SHORT side; only the engine CALL is mapped.
            mapped_side = "BUY" if side == "LONG" else "SELL"
            try:
                res = scorer(sym, side=mapped_side)
            except Exception as e:
                log.warning("cc#1548 get_primary_styles()(%s, side=%s) raised: %s", sym, mapped_side, e)
                failed += 1
                continue
            best = res.get("best") if not res.get("error") else None
            if not best or best.get("score") is None or best.get("max") in (None, 0) or best.get("score100") is None:
                failed += 1
                continue
            score, total, pct = float(best["score"]), float(best["max"]), float(best["score100"])
            # cc#1548 critical caveat (founder-flagged, do not silently smooth over): SELL-side
            # weights in tc_rule_weights are not yet live-calibrated, so a SHORT-mapped SELL tick
            # carries best_score10_weighted=False. score_pct (the unweighted score/max ratio x100)
            # is still mathematically valid, but it is NOT on the same calibrated footing as a
            # weighted BUY/LONG tick — flagged in-band, in the existing verdict_class text column,
            # rather than a new one (no ALTER TABLE, per this card's own scope).
            verdict_class = res.get("best_verdict") or "REJECT"
            if not res.get("best_score10_weighted"):
                verdict_class = f"{verdict_class} (unweighted)"
            # cc#1550: append the winning bucket (best_label, e.g. "SELL-MOM") to the SAME column —
            # column-reuse, no ALTER TABLE, same pattern this table already uses (pp reused for the
            # 3-day trailing average, cc#1539/1540). FIXED FORMAT, stated here so a future reader
            # never has to reverse-engineer it: "<VERDICT>[ (unweighted)] | <BUCKET>", e.g.
            # "VALID | SELL-MOM" or "VALID (unweighted) | SELL-MOM". " | " is the separator, it never
            # appears inside either piece, so a consumer can safely split on it (scorr_card_common.js
            # ScorrMarkerFlagDetailHtml does exactly that for the marker popover).
            best_label = res.get("best_label") or ""
            if best_label:
                verdict_class = f"{verdict_class} | {best_label}"
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_tc_score_ticks
                      (symbol, ts, side, score, total, score_pct, verdict_class)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, ts, side) DO NOTHING
                """, (sym, ts, side, score, total, pct, verdict_class))
                wrote += cur.rowcount
            scored += 1
        # 30-day rolling retention, same job (founder amendment) — 5-min ticks accumulate far
        # faster than one row/day, so the table is kept bounded here rather than left to grow.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM v8_tc_score_ticks WHERE ts < NOW() - INTERVAL '30 days'")
            purged = cur.rowcount
        conn.commit()

        # AMBER — the current tick vs the derived daily series. Today's value is each symbol's
        # LATEST tick today; each prior day's representative is that day's LAST tick (the stated
        # choice, applied consistently in both places). Rule unchanged: >80 AND rising vs the
        # 3-day trailing average, 3 full prior days required.
        amber, awrote = [], 0
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.symbol, t.side, t.score_pct, h.trail_avg, h.n_prior
                FROM (SELECT DISTINCT ON (symbol, side) symbol, side, score_pct
                      FROM v8_tc_score_ticks WHERE ts::date = %s
                      ORDER BY symbol, side, ts DESC) t
                JOIN LATERAL (
                    SELECT AVG(rep) AS trail_avg, COUNT(*) AS n_prior
                    FROM (SELECT DISTINCT ON (ts::date) score_pct AS rep
                          FROM v8_tc_score_ticks
                          WHERE symbol = t.symbol AND side = t.side AND ts::date < %s
                          ORDER BY ts::date DESC, ts DESC
                          LIMIT %s) p
                ) h ON TRUE""", (d, d, TC_TRAIL_DAYS))
            for sym, side, pct, trail, n_prior in cur.fetchall():
                pct, trail = _f(pct), _f(trail)
                if n_prior < TC_TRAIL_DAYS or pct is None or trail is None:
                    continue        # <3 prior days: cannot evaluate — skip, never fabricate
                if pct > TC_STRONG_PCT and pct > trail:
                    amber.append((sym, pct, trail))
            for sym, pct, trail in amber:
                # COLUMN REUSE, STATED: level_value carries the firing tick's score_pct and pp
                # the 3-day trailing average — the same reuse cc#1539 documents for its two SMAs;
                # the row stays self-describing through level_name='tc_score_pct'.
                # cmp_at_star/day_1d are NULL: a score marker has no price of its own.
                # First-fire-only PER DAY under the existing unique key — later ticks that still
                # qualify DO NOTHING, so first_seen_ts records when the condition first held.
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, direction, star_color,
                       level_name, level_value, pp)
                    VALUES (%s,%s,%s,'TC_STRONG','AMBER','tc_score_pct',%s,%s)
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (d, ts, sym, pct, trail))
                awrote += cur.rowcount
        conn.commit()
        out = {"ok": True, "date": str(d), "candidates": len(pos), "scored": scored,
               "tick_rows_new": wrote, "failed": failed, "purged_30d": purged,
               "amber_fired": len(amber), "amber_new_rows": awrote,
               "elapsed_ms": int((_t.monotonic() - t0) * 1000),
               # a zero-amber tick is VALID — and guaranteed for the first 3 trading days
               # (the trailing gate cannot be met until the derived daily series exists).
               "zero_amber_tick": not amber}
        log.info("cc#1540 tc_score_tick: %s", out)
        return out
    except Exception as e:
        log.exception("cc#1540 tc_score_tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def star_note(s: Dict[str, Any]) -> str:
    """Tooltip text. FACTS ONLY — no buy/sell/entry/target wording anywhere, per the card and the
    5646 reasoning. This function is the single place that copy is written, so it cannot drift."""
    td = s.get("touched_dates") or []
    when = td[-1] if td else "recently"
    side = "above" if (s.get("pct_from_level") or 0) >= 0 else "below"
    # cc#932: mom_2d is cited when present. Still FACTS ONLY — no buy/sell/entry/target wording.
    # It is absent on a row read back from the log (no such column), and the sentence simply omits
    # it rather than printing a placeholder.
    m2 = s.get("mom_2d")
    tail = f", 2-day {m2:+.1f}%" if isinstance(m2, (int, float)) else ""
    return (f"touched {s['level_name']} on {when}, now {abs(s.get('pct_from_level') or 0):.1f}% "
            f"{side} it, {'up' if s['day_1d'] >= 0 else 'down'} {abs(s['day_1d']):.1f}% today{tail}")


def run_tick(conn=None) -> Dict[str, Any]:
    """One 5-min tick: evaluate, then log FIRST FIRE ONLY.

    ON CONFLICT DO NOTHING is what makes first_seen_ts and cmp_at_star immutable — a later tick on
    the same day must never overwrite the moment the star first appeared (card item 7). That is the
    whole measurement value of the log: when it fired and at what price.
    """
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        d = _ist_now().date()
        stars = evaluate(conn, d)
        ts = _ist_now()
        wrote = 0
        with conn.cursor() as cur:
            for s in stars:
                # cc#996 ROOT CAUSE: evaluate() returns touched_dates as a list of STRINGS (for the
                # JSON API), and psycopg2 binds a Python str-list as a Postgres text[]. The column is
                # date[], and Postgres does NOT implicitly assign text[] -> date[], so EVERY blue/red
                # star (touched_dates is always present) raised "column ... is of type date[] but
                # expression is of type text[]" — swallowed by run_tick's except, leaving the table
                # empty for 5 days behind a green scheduler status. The explicit %s::date[] cast makes
                # the text[] -> date[] conversion explicit and the insert succeeds. Activity rows have
                # no touched_dates, which is why they were never the ones that failed.
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, basket, direction, star_color,
                       level_name, level_value, pp, cmp_at_star, pct_from_level, near_pp,
                       day_1d, dma_50, touched_dates)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::date[])
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (d, ts, s["symbol"], s["basket"], s["direction"], s["star_color"],
                      s["level_name"], s["level_value"], s["pp"], s["cmp_at_star"],
                      s["pct_from_level"], s["near_pp"], s["day_1d"], s["dma_50"],
                      s["touched_dates"] or None))
                wrote += cur.rowcount
        conn.commit()
        # A ZERO-STAR DAY IS VALID and is logged as such rather than silently passing — 8 of the
        # founder's 22 sampled sessions had no blue star at all (ENGINE_LIVENESS_RULE 13829: an
        # explicitly logged valid-empty outcome is evidence, silence is not).
        # cc#933: activity markers are logged in the SAME table with direction='ACTIVITY'. That
        # value cannot collide with the BUY/SELL pivot rows under the existing
        # UNIQUE(symbol, star_date, direction), so a symbol carrying both a pivot marker and an
        # activity marker logs both — and no ALTER TABLE is needed (cc#351). level_value carries
        # whichever leg fired, so the row is self-describing.
        acts = evaluate_activity(conn, d)
        awrote = 0
        with conn.cursor() as cur:
            for a in acts:
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, basket, direction, star_color,
                       level_name, level_value, cmp_at_star, day_1d)
                    VALUES (%s,%s,%s,%s,'ACTIVITY','GREEN',%s,%s,NULL,NULL)
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (d, ts, a["symbol"], None, a["trigger"],
                      # cc#1441: level_value carries the vol value that fired (RVOL first, else
                      # VOL P) for VOL / VOL+OI triggers, the OI figure for pure OI — the row
                      # stays self-describing under the amended legs.
                      (a["rvol"] if a["rvol"] is not None else a["vol_p"])
                      if a["trigger"] != "OI" else a["oi_dod_pct"]))
                awrote += cur.rowcount
        conn.commit()
        # cc#1539: the third family, same first-fire-only pattern. direction values
        # DMA_CROSS_UP / DMA_CROSS_DOWN cannot collide with BUY/SELL/ACTIVITY rows under the
        # existing UNIQUE(symbol, star_date, direction) — no ALTER TABLE (cc#351).
        # COLUMN REUSE, STATED: level_value carries the 5DMA and pp carries the 20DMA. pp is a
        # pivot-point column by name, but adding a column needs a locked weekend window; the row
        # stays self-describing through level_name='5DMA_X_20DMA'.
        dmas = evaluate_dma_cross(conn, d)
        dwrote = 0
        with conn.cursor() as cur:
            for x in dmas:
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, basket, direction, star_color,
                       level_name, level_value, pp, cmp_at_star, day_1d)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (d, ts, x["symbol"], x["basket"], x["direction"], x["star_color"],
                      x["level_name"], x["level_value"], x["pp"], x["cmp_at_star"], x["day_1d"]))
                dwrote += cur.rowcount
        conn.commit()
        log.info("cc#856/933/1539 pivot_star tick: %d pivot markers (%d new), %d activity (%d new), "
                 "%d dma crosses (%d new)%s",
                 len(stars), wrote, len(acts), awrote, len(dmas), dwrote,
                 " (VALID ZERO-MARKER TICK)" if not stars and not acts and not dmas else "")
        return {"ok": True, "date": str(d), "evaluated": len(stars), "new_rows": wrote,
                "activity": len(acts), "activity_new_rows": awrote,
                "dma_cross": len(dmas), "dma_cross_new_rows": dwrote,
                "zero_star_tick": not stars and not acts and not dmas, "scope": EVAL_SCOPE}
    except Exception as e:
        log.exception("cc#856 pivot_star tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


@router.get("/api/v8/pivot_star")
def pivot_star(star_date: Optional[str] = None):
    """The latest session's starred symbols. Read-only; never triggers a write.

    cc#1032: the default used to be TODAY, and on a Saturday there are no rows for today — so the
    markers vanished from the dashboard every weekend and holiday while the tables beside them still
    showed last session's positions at last session's prices. The client wiring was never broken.
    The default is now the last session that actually HAS rows, which is the cc#424 last-session
    as-of doctrine the funnel already follows. An explicit star_date still overrides everything.
    """
    try:
        with _conn() as conn, conn.cursor() as cur:
            d = star_date
            _today = str(_ist_now().date())
            if not d:
                # The ceiling is the IST date, NOT Postgres CURRENT_DATE. The Railway session is
                # UTC, so between 18:30 and midnight IST CURRENT_DATE is still yesterday — using it
                # would hide the markers written during the session that just ended. Same class of
                # bug as cc#844 / cc#1022; one IST comparison avoids it.
                cur.execute("SELECT MAX(star_date) FROM v8_pivot_star_log WHERE star_date <= %s",
                            (_today,))
                row = cur.fetchone()
                d = str(row[0]) if row and row[0] else _today
            # BOTH reads below - the BLUE/RED pivot query and the GREEN activity query - use this
            # one resolved date, so the two lists can never describe different sessions.
            # cc#1008: `stars` is the PIVOT list (BLUE/RED) only. GREEN activity rows live in the
            # same table but render via the `activity` list below — leaving them in `stars` made
            # pivotStar/pivotMark (which map star_color as BLUE?blue:red) paint every green marker
            # RED on both surfaces (the founder's GRASIM). Filtering them here fixes both copies at
            # the one shared source, with no frontend colour change.
            # cc#1539: the colour filter alone no longer discriminates marker families — DMA-cross
            # rows are GREEN/RED too. `direction` is the family key (the table's own doctrine), so
            # BOTH this read and the activity/dma reads below filter on it; without this, a red
            # DMA cross would render as a red pivot star.
            cur.execute("""
                SELECT symbol, basket, direction, star_color, level_name, level_value,
                       pct_from_level, day_1d, near_pp, cmp_at_star, first_seen_ts, touched_dates
                FROM v8_pivot_star_log WHERE star_date=%s AND direction IN ('BUY','SELL')
                ORDER BY star_color, symbol""", (d,))
            # cc#932: `glyph` is DERIVED from the stored direction, so it needs no new column and
            # no ALTER TABLE (MAINTENANCE_LOCK_RULE cc#351 forbids one here). Every existing field
            # is untouched, so the response stays backward-compatible — consumers that ignore
            # `glyph` keep working exactly as before.
            rows = [{
                "symbol": r[0], "basket": r[1], "direction": r[2], "star_color": r[3],
                "level_name": r[4], "level_value": _f(r[5]), "pct_from_level": _f(r[6]),
                "day_1d": _f(r[7]), "near_pp": r[8], "cmp_at_star": _f(r[9]),
                "first_seen_ts": str(r[10]) if r[10] else None,
                "touched_dates": [str(x) for x in (r[11] or [])],
                "glyph": GLYPH.get(r[2], "star"),
                "spec_version": "V2",
            } for r in cur.fetchall()]
            for r in rows:
                r["note"] = star_note(r)
            # cc#933: activity markers are a SEPARATE list, not merged into `stars`. A symbol can
            # carry a pivot marker AND an activity marker at the same time; one keyed map would
            # silently drop whichever came second.
            # cc#1008: read GREEN markers for DISPLAY from the LOG (persisted at first fire), NOT a
            # live re-evaluation. run_tick() still DETECTS and WRITES green rows via
            # evaluate_activity() (18053 scope/side rules intact; volume legs amended to the canon
            # RVOL/VOL P pair by cc#1441 per session_log 33843) — but a live re-eval at render time can
            # fade intra-day and drop a marker the log still holds, so the surface would disagree
            # with v8_pivot_star_log. Reading the log makes what BOTH surfaces render match the table
            # exactly (the founder's own verify + DISPLAY_PARITY 16202). cc#1024: the glyph is a
            # lightning bolt on both sides and no longer follows the position side at all; note is
            # rebuilt facts-only from the logged trigger + value, same wall as star_note (no
            # buy/sell/entry/target wording).
            acts = []
            try:
                with conn.cursor() as acur:
                    acur.execute("""
                        SELECT g.symbol, g.level_name, g.level_value, p.side
                        FROM v8_pivot_star_log g
                        LEFT JOIN LATERAL (
                            SELECT side FROM v8_paper_positions
                            WHERE symbol = g.symbol AND status='OPEN'
                            ORDER BY entry_ts DESC LIMIT 1
                        ) p ON TRUE
                        WHERE g.star_date=%s AND g.direction='ACTIVITY'
                        ORDER BY g.symbol""", (d,))
                    for sym, lname, lval, side in acur.fetchall():
                        side_u = (side or "").upper()
                        lv = _f(lval)
                        trig = (lname or "").upper()
                        facts = []
                        # cc#1441: "usual pace" wording is honest for BOTH eras of logged rows —
                        # old rows hold the retired v8_metrics day ratio, new rows hold RVOL/VOL P.
                        if trig in ("VOL", "VOL+OI") and lv is not None:
                            facts.append(f"volume {lv:.1f}x its usual pace")
                        if trig == "OI" and lv is not None:
                            facts.append(f"OI {lv:+.0f}% day-over-day")
                        elif trig == "VOL+OI":
                            facts.append("OI event day-over-day")
                        acts.append({
                            "symbol": sym, "side": side_u or None,
                            # cc#1024: the bolt renders as an emoji in its own colours, so no colour
                            # is served for it. star_color stays GREEN because it is the LOG's own
                            # row value (v8_pivot_star_log.star_color) and this endpoint reports the
                            # table as it is — renaming a stored value to match a glyph change would
                            # make the payload disagree with the row it came from.
                            "star_color": "GREEN", "color": None,
                            "glyph": GLYPH_SIDE.get(side_u, "bolt"),
                            "level_name": lname, "level_value": lv,
                            "note": " · ".join(facts) if facts else "unusual activity",
                        })
            except Exception as e:
                log.warning("cc#1008 activity log-read failed: %s", e)
                acts = []
            # cc#1539: DMA crosses are the THIRD list — LOG read, not a live re-eval, for the
            # exact cc#1008 reason documented above: a render-time re-eval could disagree with
            # what the log holds (and a cross is only "fresh" on the session it fired, so a later
            # re-eval would drop a marker the log correctly keeps for the day).
            dma = []
            try:
                with conn.cursor() as dcur:
                    dcur.execute("""
                        SELECT symbol, direction, star_color, level_value, pp, cmp_at_star
                        FROM v8_pivot_star_log
                        WHERE star_date=%s AND direction IN ('DMA_CROSS_UP','DMA_CROSS_DOWN')
                        ORDER BY symbol""", (d,))
                    for sym, dirn, col, lv, ppv, cmpv in dcur.fetchall():
                        rel = "above" if dirn == "DMA_CROSS_UP" else "below"
                        lvf, ppf = _f(lv), _f(ppv)
                        dma.append({
                            "symbol": sym, "direction": dirn, "star_color": col,
                            "glyph": "square",
                            "level_name": "5DMA_X_20DMA",
                            "level_value": lvf, "pp": ppf,
                            "cmp_at_star": _f(cmpv),
                            # FACTS ONLY — same wall as star_note().
                            "note": (f"5DMA {lvf:,.2f} crossed {rel} 20DMA {ppf:,.2f}"
                                     if lvf is not None and ppf is not None
                                     else f"5DMA crossed {rel} 20DMA"),
                        })
            except Exception as e:
                log.warning("cc#1539 dma-cross log-read failed: %s", e)
                dma = []
            # cc#1540: TC_STRONG amber stars — the fourth list, LOG read like the others.
            tcs = []
            try:
                with conn.cursor() as tcur:
                    tcur.execute("""
                        SELECT symbol, level_value, pp FROM v8_pivot_star_log
                        WHERE star_date=%s AND direction='TC_STRONG'
                        ORDER BY symbol""", (d,))
                    for sym, pct, trail in tcur.fetchall():
                        pctf, trailf = _f(pct), _f(trail)
                        tcs.append({
                            "symbol": sym, "direction": "TC_STRONG", "star_color": "AMBER",
                            "glyph": "star",
                            "score_pct": pctf, "trail_avg_3d": trailf,
                            # FACTS ONLY — same wall as every other marker note.
                            "note": (f"Trade Check {pctf:.0f}%, above its 3-day average {trailf:.0f}%"
                                     if pctf is not None and trailf is not None
                                     else "Trade Check above 80% and rising vs its 3-day average"),
                        })
            except Exception as e:
                log.warning("cc#1540 tc_strong log-read failed: %s", e)
                tcs = []
            return {
                # cc#1032: the RESOLVED date, so every surface is honest about which session it is
                # showing, plus an explicit flag rather than making a reader compare dates.
                "star_date": d, "as_of_is_last_session": (d != _today),
                "count": len(rows), "stars": rows,
                "activity": acts, "activity_count": len(acts),
                "dma_cross": dma, "dma_cross_count": len(dma),
                "tc_strong": tcs, "tc_strong_count": len(tcs),
                "scope": EVAL_SCOPE,
                "rule": ("PIVOT_STAR_V2 (session_log 18052) with MARKER_GLYPH_V3 glyphs "
                         "(session_log 21764), evaluated on the OPEN paper book. "
                         "BLUE STAR (buy side): touched its own S1 in the last 3 closed sessions; "
                         "cmp within 2% above S1 or above PP; day_1d and mom_2d both between -1 "
                         "and +2. RED STAR (sell side): mirrored on R1 — touched R1, cmp within "
                         "2% below R1 or below PP, day_1d and mom_2d both between -2 and +1. Both "
                         "sides draw a star; the colour carries the side, not the shape."),
                "spec_version": "V2",
                "spec_version_note": ("v8_pivot_star_log has no free text column for a version "
                                      "stamp and ALTER TABLE is not permitted from here "
                                      "(MAINTENANCE_LOCK_RULE cc#351). It needs none: the table "
                                      "held ZERO rows at the moment V2 shipped (cc#931 — V1 never "
                                      "wrote once), so every row in it is V2 by construction."),
                "basis": ("DISPLAY MARKER + MEASUREMENT LOG ONLY. Not a qualification rule, not a "
                          "basket, not a slot; it never creates a paper entry (session_log 5646)."),
                "near_pp_note": "near_pp is recorded for a 4-6 week review only and is never rendered.",
                # cc#933: the legend copy lives HERE so /dashboard and /m/v8 cannot word it
                # differently. Both surfaces render the shared snippet, which reads this.
                # cc#1018 (21764) made the amber marker STRONG-only and both pivot sides stars.
                # cc#1024 MARKER_GLYPH_V5 (22296) turns the activity marker into a lightning bolt
                # and DELETES the shape-by-side line outright — there is no marker left whose shape
                # depends on which way you are positioned, so a footer explaining that rule would be
                # explaining something that no longer happens. Four rows, one per marker.
                # cc#1540: the amber line now states the REAL implemented condition — the old
                # "STRONG / VALID" wording described nothing that ran and is replaced, not kept
                # alongside (the card's own instruction: dead copy must not survive next to live).
                "legend": [
                    "Amber star = Trade Check score above 80% and rising vs its 3-day average",
                    "Blue star = held reversal at S1",
                    "Red star = mirror at R1",
                    "⚡ = Volume/OI spurt · volume >1.5x or OI >25% day-over-day",
                    "Green/red square = fresh 5DMA cross above/below 20DMA",
                ],
            }
    except Exception as e:
        log.exception("pivot_star endpoint failed")
        return {"star_date": star_date, "stars": [], "count": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.get("/api/v8/tc_score_latest")
def tc_score_latest():
    """cc#1542: the CURRENT Trade Check score for every open-book position — the dashboard's
    TC % column. Read-only over v8_tc_score_ticks (cc#1540/1541): the most recent tick per
    (symbol, side), scoped to the same open book every marker evaluator uses. A symbol with no
    ticks yet is simply ABSENT — never a fabricated value. Distinct from pivot_star's tc_strong
    list, which only carries symbols where the AMBER condition fired; this returns the raw score
    for every row, fired or not."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            _retired, _ = retired_baskets(cur)
            cur.execute("""
                WITH book AS (
                    SELECT DISTINCT ON (p.symbol) p.symbol, UPPER(COALESCE(p.side,'LONG')) AS side
                    FROM v8_paper_positions p
                    LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
                    WHERE p.status = 'OPEN'
                      AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
                      AND NOT (p.basket = ANY(%(retired)s))
                    ORDER BY p.symbol, p.entry_ts DESC
                )
                SELECT b.symbol, b.side, t.score_pct, t.verdict_class, t.ts
                FROM book b
                JOIN LATERAL (
                    SELECT score_pct, verdict_class, ts FROM v8_tc_score_ticks
                    WHERE symbol = b.symbol AND side = b.side
                    ORDER BY ts DESC LIMIT 1
                ) t ON TRUE
                ORDER BY b.symbol""", {"retired": _retired})
            rows = [{"symbol": r[0], "side": r[1],
                     "score_pct": _f(r[2]), "verdict_class": r[3],
                     "ts": str(r[4]) if r[4] else None} for r in cur.fetchall()]
        return {"rows": rows, "count": len(rows)}
    except Exception as e:
        log.exception("cc#1542 tc_score_latest failed")
        return {"rows": [], "count": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.post("/api/v8/pivot_star/run")
def pivot_star_run():
    """Manual tick (ops/verification). The scheduled 5-min job calls run_tick() directly."""
    return run_tick()
