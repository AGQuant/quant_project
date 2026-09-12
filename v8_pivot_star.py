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
# cc#1811 V8_MARKER_VOLUME_CANON_LINK_V1 (session_log 40489, founder 07-Sep-2026, supersedes
# cc#1810): the ANY-leg-trips vol>X-OR-OI>Y% test below is RETIRED. It is replaced entirely by the
# canon Vol R/P/D/AD four-check tally Trade Check already scores (tc_v4_dual.py's _vol_reads /
# _vol_checks, cc#1785/1786) — see evaluate_activity() further down for the live implementation.
# ONE volume definition in the codebase now, not a bespoke second one living only here. The
# constants below are KEPT, not deleted (matching this module's own convention for a superseded
# value — e.g. `_direction` above), because they document what the marker used to test; nothing in
# this file reads them any more.
#
# THE RETIRED TEST, for history: fires when ANY leg trips:
#   (a) RVOL  > ACTIVITY_RVOL_X  — today's slot-normalized pace (rvol_engine, profile read)
#   (b) VOL P > ACTIVITY_VOLP_Y  — the prior session's closing RVOL (same formula, prior day)
#   (c) |OI day-over-day| > 25%  — futures_basis, last tick of the day vs last tick of the prior
#       session. This OI leg is what cc#1811 removes outright — not deprioritised, retired: no
#       futures_basis read remains anywhere in evaluate_activity() after this card.
#
# READ 18053 CAREFULLY — two of its keys look contradictory and are not. `founder_amendment_08aug`
# says there is NO side split; `founder_final_08aug` says BUY shows a star and SELL a circle. They
# reconcile cleanly: the CONDITION has no side split (identical test both sides, no mirrored band),
# while the GLYPH does. One condition, one meaning — unusual activity — drawn in the shape of the
# side it sits on. cc#1811 keeps this: the new 4-check tally is likewise side-aware only on the
# Vol AD leg (Accumulation for LONG, Distribution for SHORT, exactly as Trade Check reads it), and
# the glyph rule below is untouched.
#
# AND THE SIDE HERE IS THE POSITION'S OWN SIDE — the opposite of cc#932. That is deliberate, not an
# inconsistency: a pivot marker describes how the STOCK is behaving against its own levels (so
# MAXHEALTH can carry a SHORT position and a BUY star), whereas activity has no directional reading
# at all — volume and OI say "something is happening", not "up" or "down". So the only side it can
# honestly take is the side you are on. DO NOT unify these two side rules later; they answer
# different questions.
# RETIRED cc#1811 — kept for history only, no longer read anywhere in this file:
ACTIVITY_RVOL_X      = 1.5   # was FINAL — 30-Aug-2026 sign-off round (cc#1441)
ACTIVITY_VOLP_Y      = 1.5   # was FINAL — 30-Aug-2026 sign-off round (cc#1441)
ACTIVITY_OI_DOD_PCT  = 25.0  # was untouched (18053); the OI leg itself is gone, cc#1811

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
def _fetch_star_support(cur, d, syms):
    """cc#1978 items 5/8 refactor: the batched supporting reads the star condition needs (pivots,
    live CMP, EOD close, v8_metrics, the touch-test CTE) — factored out of evaluate() so a
    universe-scoped caller (evaluate_universe(), below) reads the identical way over a different
    candidate set, not a second copy of this SQL. Pure extraction — every query here is
    byte-identical to what evaluate() ran inline before this refactor."""
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

    return piv, live, eod, met, touch


def _score_stars(cands, piv, live, eod, met, touch):
    """cc#1978 items 5/8 refactor: the star CONDITION math itself, pure (no DB access) — factored
    out of evaluate() so evaluate_universe() reuses the EXACT same rule rather than a duplicated
    copy (the constraint v8_marker_ticks.py's own docstring names for this exact refactor). Every
    line below (through the star check) is byte-identical to evaluate()'s own former inline loop —
    nothing about the star definition changed by this extraction.

    Returns (fired, evaluated_syms) — `fired` is the ORIGINAL single-list contract (only symbols
    that got a star), unchanged, so evaluate()/evaluate_universe() keep their existing return
    shape exactly. `evaluated_syms` is additive: the set of symbols that cleared the DATA gates
    (had a pivot, metrics, and a cmp) and so were genuinely put through the star check, whether or
    not one fired — this is what v8_marker_ticks.persist_star_ticks needs to write real
    fired=false rows (a symbol skipped for MISSING data never reached a real check, so it is
    correctly excluded from this set too — no row, matching "did not run" for that tick, the same
    convention persist_chan_ticks already uses for a symbol compute_channels() has no fit for)."""
    def _band(v, lo_hi):
        return v is not None and lo_hi[0] <= v <= lo_hi[1]

    out = []
    evaluated_syms = set()
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
        evaluated_syms.add(sym)   # cleared every data gate above -- a real check happens below
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
    return out, evaluated_syms


def evaluate(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """Return today's stars. PURE READ — this function writes nothing.

    cc#1978 items 5/8 refactor (unchanged behaviour): the candidate-set query below is exactly
    what it was before this refactor — EVAL_SCOPE stays "positions", session_log 18052's lock is
    untouched, every existing consumer of this function sees identical output. Only the supporting
    fetch and the condition math moved into _fetch_star_support/_score_stars, shared with the new
    evaluate_universe() below rather than duplicated for it."""
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
        piv, live, eod, met, touch = _fetch_star_support(cur, d, syms)
    fired, _evaluated = _score_stars(cands, piv, live, eod, met, touch)
    return fired


def evaluate_universe(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """cc#1978 items 5/8: the star marker evaluated against the FULL active futures registry, not
    the open book. Authorized explicitly and separately from the book-scoped lock: founder word
    10-Sep 22:05 (FOUNDER_WORD_10SEP_2205_STARS_UNIVERSE, quoted verbatim on cc#1978's own spec —
    "just like TC score, calculate the flags also for all the future symbols and distribute
    everywhere"), an explicit dated override of session_log 18052's EVAL_SCOPE="positions" lock
    for this one purpose, reconfirmed 12-Sep (cc_task_logs 6382, "continue items 2-9 of the
    card"). Does NOT touch EVAL_SCOPE or evaluate() — the book-scoped marker and every page that
    reads it behave exactly as before this function exists. Reuses _fetch_star_support/
    _score_stars verbatim, the identical condition math evaluate() uses (v8_marker_ticks.py's own
    docstring names "a shared helper, not a duplicated copy" as the required shape). PURE READ."""
    d = target_date or _ist_now().date()
    with conn.cursor() as cur:
        cur.execute("""SELECT symbol, NULL::text AS basket FROM futures_universe WHERE is_active""")
        cands = [(r[0], r[1]) for r in cur.fetchall()]
        if not cands:
            return []
        syms = [c[0] for c in cands]
        piv, live, eod, met, touch = _fetch_star_support(cur, d, syms)
    fired, _evaluated = _score_stars(cands, piv, live, eod, met, touch)
    return fired


def evaluate_universe_with_state(conn, target_date: Optional[date] = None):
    """cc#1978 item 8: same as evaluate_universe() but also returns which symbols cleared the
    data gates (evaluated_syms) regardless of whether a star fired — v8_marker_ticks.
    persist_star_ticks needs this to write real fired=false rows (the card's own verify V2:
    "fired=false rows must exist and outnumber fired=true"). evaluate_universe() itself keeps its
    plain single-list contract for any other caller; this variant exists so that need does not
    force a signature change on the plain one. Returns (fired: List[Dict], evaluated_syms: set)."""
    d = target_date or _ist_now().date()
    with conn.cursor() as cur:
        cur.execute("""SELECT symbol, NULL::text AS basket FROM futures_universe WHERE is_active""")
        cands = [(r[0], r[1]) for r in cur.fetchall()]
        if not cands:
            return [], set()
        syms = [c[0] for c in cands]
        piv, live, eod, met, touch = _fetch_star_support(cur, d, syms)
    return _score_stars(cands, piv, live, eod, met, touch)


def evaluate_activity(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """GREEN activity markers on the OPEN book (cc#933 / session_log 18053). PURE READ.

    cc#1811 (session_log 40489, supersedes cc#1810): condition is now the canon Vol R/P/D/AD
    four-check tally (>=2 of 4 pass), reusing tc_v4_dual.py's exact r6_read/deliv_ratio_batch/
    _ad_21d imports and thresholds — not a second volume definition. The OI-change leg is retired
    outright, not just deprioritised: no futures_basis read remains in this function.

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

        # cc#1811 V8_MARKER_VOLUME_CANON_LINK_V1: the SAME three functions tc_v4_dual.py's
        # _vol_reads() calls for Trade Check's R5/R5V — r6_read (live 3-tier RVOL/VOL-P),
        # deliv_ratio_batch (already batch-form — one call for the whole open book, not a
        # per-symbol loop), _ad_21d (no batch form; open-book scale here is ~20-25 symbols, the
        # same order Trade Check already loops one-at-a-time for its own single-symbol page, so a
        # new batch wrapper is not worth adding for this size — the card's own "add one only if
        # needed" clause). NOT a parallel calculation: these are the identical imports, called the
        # identical way, thresholds imported from tc_v4_dual too rather than retyped here.
        #
        # KNOWN LIMITATION, stated rather than hidden: r6_read/deliv_ratio_batch/_ad_21d all read
        # the LATEST live/EOD tables — none takes a historical-date parameter the way the retired
        # day_rvol_batch(cur, syms, _days[0]) read did. `target_date` (and therefore `d`) no longer
        # affects the volume test at all; it still scopes which OPEN positions are evaluated. In
        # practice this function only ever runs live on the current session (the module's own
        # "TICK SERIES — every 5 minutes" cadence), so this is not a live behaviour change — but a
        # future replay call passing a past target_date would score that day's positions against
        # TODAY's volume, not that day's. Flagged here rather than silently accepted.
        from r6_volume import r6_read
        from volume_flow_endpoints import deliv_ratio_batch
        from deriv_metrics import _ad_21d
        from tc_v4_dual import VOL_R_MIN, VOL_P_MIN, VOL_D_MIN, VOL_AD_MIN, VOL_AD_MAX_DIST

        deliv = deliv_ratio_batch(cur, syms)
        vol_reads = {}
        for sym in syms:
            try:
                rv = r6_read(cur, sym) or {}
            except Exception as e:
                log.warning("cc#1811 r6_read %s: %s", sym, e)
                rv = {}
            try:
                ad = _ad_21d(cur, sym) or {}
            except Exception as e:
                log.warning("cc#1811 _ad_21d %s: %s", sym, e)
                ad = {}
            vol_reads[sym] = {"vol_r": _f(rv.get("rvol")), "vol_p": _f(rv.get("vol_p")),
                               "vol_d": _f(deliv.get(sym)), "vol_ad": _f(ad.get("up_vol_pct"))}

    out = []
    for sym, side in pos:
        vr = vol_reads.get(sym, {})
        r_v, p_v, d_v, ad_v = vr.get("vol_r"), vr.get("vol_p"), vr.get("vol_d"), vr.get("vol_ad")
        r_ok = r_v is not None and r_v >= VOL_R_MIN
        p_ok = p_v is not None and p_v >= VOL_P_MIN
        d_ok = d_v is not None and d_v >= VOL_D_MIN
        # side-aware exactly as tc_v4_dual._vol_checks reads it: Accumulation for LONG, Distribution
        # for SHORT — this module's `side` is already the position's own side (v8_paper_positions.side).
        ad_ok = ad_v is not None and ((ad_v <= VOL_AD_MAX_DIST) if side == "SELL" else (ad_v >= VOL_AD_MIN))
        passed = int(r_ok) + int(p_ok) + int(d_ok) + int(ad_ok)
        if passed < 2:
            continue
        checks = [
            {"key": "vol_r", "label": "Vol R", "value": r_v, "pass": r_ok},
            {"key": "vol_p", "label": "Vol P", "value": p_v, "pass": p_ok},
            {"key": "vol_d", "label": "Vol D", "value": d_v, "pass": d_ok},
            {"key": "vol_ad", "label": "Vol AD", "value": ad_v, "pass": ad_ok},
        ]
        # FACTS ONLY, same wall as star_note(): no buy/sell/entry/target wording. Names every
        # passed check with its value so the tooltip answers "why" (cc#1811 step 3), not just
        # yes/no — same transparency pattern the other markers on this board already use.
        facts = [f"{c['label']} {c['value']:.2f}" for c in checks if c["pass"] and c["value"] is not None]
        out.append({
            "symbol": sym, "side": side,
            "star_color": "GREEN",
            "glyph": GLYPH_SIDE.get(side, "star"),
            "checks_passed": passed,
            "checks": checks,
            "note": " · ".join(facts) + f" ({passed}/4 checks)",
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
#
# cc#1682 (founder direct 04-Sep, absorbs cc#1681): SUPERSEDES the cross-only definition above.
# evaluate_dma_cross() and its DMA_CROSS_UP/DMA_CROSS_DOWN rows are LEFT IN PLACE (old rows are
# never deleted, do_not_touch) but the endpoint stops reading them — evaluate_dma_state() below is
# what run_tick() and the post-close pass call now. State, not cross: every open card shows its
# square every day the relationship holds, not just the day it flipped.
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


# ── cc#1682 DMA_STATE_V1 (founder direct 04-Sep) — STATE, not cross ───────────────────────────
# The founder's ask: every open card carries one square every day, green when 5DMA sits above
# 20DMA, red when below — not only on the session the relationship flips. Same candidate set, same
# SMA math as evaluate_dma_cross above; the only real difference is dropping yesterday's SMA pair
# (a state test needs none) and lowering the history floor from DMA_SLOW+1 to DMA_SLOW, since
# there is no "yesterday" comparison left to need the extra day.
def _fetch_dma_state_support(cur, d, syms):
    """cc#1978 items 5/8 refactor: the batched supporting reads the DMA state needs (close
    history + dates, live CMP, day_1d) — factored out of evaluate_dma_state() so
    evaluate_dma_state_universe() (below) reads the identical way over a different candidate set.
    Pure extraction — byte-identical to what evaluate_dma_state() ran inline before this refactor."""
    ceiling_op = "<" if _ist_market_hours() else "<="
    cur.execute(f"""
        SELECT symbol, price_date, close FROM (
            SELECT symbol, price_date, close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
            FROM raw_prices
            WHERE symbol = ANY(%s) AND close > 0 AND price_date {ceiling_op} %s
        ) x WHERE rn <= %s
        ORDER BY symbol, rn""", (syms, d, DMA_FETCH))
    closes: Dict[str, List[float]] = {}
    dates: Dict[str, List[date]] = {}
    for sym, pdate, close in cur.fetchall():
        closes.setdefault(sym, []).append(float(close))
        dates.setdefault(sym, []).append(pdate)

    live = {}
    try:
        import cmp_resolver
        live = cmp_resolver.resolve_cmp_many(cur, syms)
    except Exception as e:
        log.warning("cc#1682 live CMP unavailable, using last close: %s", e)

    cur.execute("""SELECT DISTINCT ON (symbol) symbol, day_1d FROM v8_metrics
                   WHERE symbol = ANY(%s) AND score_date <= %s
                   ORDER BY symbol, score_date DESC""", (syms, d))
    met = {r[0]: _f(r[1]) for r in cur.fetchall()}

    return closes, dates, live, met


def _score_dma_state(cands, closes, dates, live, met):
    """cc#1978 items 5/8 refactor: the DMA-state CONDITION math itself, pure (no DB access) —
    factored out of evaluate_dma_state() so evaluate_dma_state_universe() reuses the exact same
    rule. Byte-identical to evaluate_dma_state()'s own former inline loop."""
    out = []
    for sym, basket in cands:
        c = closes.get(sym) or []          # newest first
        dts = dates.get(sym) or []
        if len(c) < DMA_SLOW:
            continue                        # insufficient history — never a guessed state
        sma5_t = sum(c[0:DMA_FAST]) / DMA_FAST
        sma20_t = sum(c[0:DMA_SLOW]) / DMA_SLOW
        if sma5_t > sma20_t:
            colour, direction, rel = "GREEN", "DMA_ABOVE", "above"
        elif sma5_t < sma20_t:
            colour, direction, rel = "RED", "DMA_BELOW", "below"
        else:
            continue                        # exact tie — genuinely undefined, never guessed
        data_date = dts[0]                  # the actual latest close used, may lag target_date
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
            "data_date": data_date,
            # FACTS ONLY, same wall as star_note(): no buy/sell/entry/target wording.
            "note": f"5DMA {sma5_t:,.2f} {rel} 20DMA {sma20_t:,.2f} (as of {data_date.strftime('%d-%b')})",
        })
    return out


def evaluate_dma_state(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """GREEN/RED square STATE markers on the OPEN book (cc#1682). PURE READ.

    Unlike evaluate_dma_cross, this reports the CURRENT relationship regardless of whether it is
    fresh. `data_date` on each returned row is the date of the LATEST close actually used, which
    during market hours is yesterday's close (today's raw_prices row is a partial candle and is
    excluded, same ceiling_op gate as the cross evaluator) — this is what lets an intraday tick and
    the post-close pass both call this function and each stamp the correct star_date without
    guessing: the caller inserts under row['data_date'], not blindly under target_date. Fewer than
    DMA_SLOW completed closes skips the symbol — insufficient history, never a guessed state.

    cc#1978 items 5/8 refactor (unchanged behaviour): this candidate query never read EVAL_SCOPE
    (v8_marker_ticks.py's own docstring notes exactly this — "a separate hardcoded open-book
    SELECT"), so there is no lock to override here; the fetch and condition math moved into
    _fetch_dma_state_support/_score_dma_state, shared with evaluate_dma_state_universe() below."""
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
        closes, dates, live, met = _fetch_dma_state_support(cur, d, syms)
    return _score_dma_state(cands, closes, dates, live, met)


def evaluate_dma_state_universe(conn, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """cc#1978 items 5/8: DMA state evaluated against the FULL active futures registry. Same
    authorization as evaluate_universe() above (FOUNDER_WORD_10SEP_2205_STARS_UNIVERSE covers
    "the flags" generally, not just the star marker — reconfirmed 12-Sep, cc_task_logs 6382).
    Does NOT touch evaluate_dma_state() — the book-scoped state marker behaves exactly as before.
    Reuses _fetch_dma_state_support/_score_dma_state verbatim. PURE READ."""
    d = target_date or _ist_now().date()
    with conn.cursor() as cur:
        cur.execute("""SELECT symbol, NULL::text AS basket FROM futures_universe WHERE is_active""")
        cands = [(r[0], r[1]) for r in cur.fetchall()]
        if not cands:
            return []
        syms = [c[0] for c in cands]
        closes, dates, live, met = _fetch_dma_state_support(cur, d, syms)
    return _score_dma_state(cands, closes, dates, live, met)


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
        # cc#1887 P0 FIX: cc#1811 rewrote evaluate_activity()'s return shape to the Vol R/P/D/AD
        # four-check tally (checks_passed/checks/note) but left THIS write loop consuming the OLD
        # pre-cc#1811 shape (a["trigger"]/a["rvol"]/a["vol_p"]/a["oi_dod_pct"]) — none of those keys
        # exist on the new dict, so a["trigger"] raised KeyError('trigger') on every single tick
        # since cc#1811 shipped (scheduler_master: bg_pivot_star active=true, last_status=error).
        # pivot_star() below reads the GREEN activity marker from this LOG, not a live re-eval
        # (cc#1008 DISPLAY_PARITY), so the failure silently froze the lightning-bolt marker on the
        # V8 dashboard at whatever last logged before the KeyError started — not merely a scheduler
        # red light. level_name='VOL_4CHECK' is a new, self-describing trigger value (same pattern
        # as the existing VOL/OI/VOL+OI legs) so no ALTER TABLE is needed (cc#351); level_value
        # carries checks_passed (2-4, the row's own minimum to log per evaluate_activity's own
        # `if passed < 2: continue` gate).
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
                """, (d, ts, a["symbol"], None, "VOL_4CHECK", a["checks_passed"]))
                awrote += cur.rowcount
        conn.commit()
        # cc#1682: the third family, now a STATE not a cross (supersedes cc#1539's fresh-cross-only
        # evaluate_dma_cross — that function and its DMA_CROSS_UP/DMA_CROSS_DOWN rows are left in
        # place untouched, do_not_touch, the endpoint just stops reading them). direction values
        # DMA_ABOVE / DMA_BELOW cannot collide with BUY/SELL/ACTIVITY/DMA_CROSS_* rows under the
        # existing UNIQUE(symbol, star_date, direction) — no ALTER TABLE (cc#351).
        # COLUMN REUSE, STATED: level_value carries the 5DMA and pp carries the 20DMA. pp is a
        # pivot-point column by name, but adding a column needs a locked weekend window; the row
        # stays self-describing through level_name='5DMA_X_20DMA'.
        #
        # star_date is each row's OWN data_date, NOT the tick's calendar date `d` — during market
        # hours today's raw_prices row is a partial candle and is excluded (_ist_market_hours gate
        # inside evaluate_dma_state), so data_date is yesterday's close until the post-close pass
        # (run_dma_state_eod) runs after today's EOD row lands. This is what makes the ON CONFLICT
        # DO NOTHING dedupe on (symbol, data_date, direction) correct: every intraday tick before
        # the close computes the identical state off the identical closes and no-ops after the
        # first; the post-close pass then inserts a genuinely NEW row under today's date.
        dmas = evaluate_dma_state(conn, d)
        dwrote = 0
        with conn.cursor() as cur:
            for x in dmas:
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, basket, direction, star_color,
                       level_name, level_value, pp, cmp_at_star, day_1d)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (x["data_date"], ts, x["symbol"], x["basket"], x["direction"], x["star_color"],
                      x["level_name"], x["level_value"], x["pp"], x["cmp_at_star"], x["day_1d"]))
                dwrote += cur.rowcount
        conn.commit()
        log.info("cc#856/933/1682 pivot_star tick: %d pivot markers (%d new), %d activity (%d new), "
                 "%d dma states (%d new)%s",
                 len(stars), wrote, len(acts), awrote, len(dmas), dwrote,
                 " (VALID ZERO-MARKER TICK)" if not stars and not acts and not dmas else "")
        return {"ok": True, "date": str(d), "evaluated": len(stars), "new_rows": wrote,
                "activity": len(acts), "activity_new_rows": awrote,
                "dma_state": len(dmas), "dma_state_new_rows": dwrote,
                "dma_cross": len(dmas), "dma_cross_new_rows": dwrote,   # alias, one release (cc#1682 scope 4)
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


def run_dma_state_eod(conn=None) -> Dict[str, Any]:
    """cc#1682 POST-CLOSE pass. Every 5-min intraday tick already writes a DMA state row, but
    during market hours it is dated on YESTERDAY's close (today's raw_prices row is a partial
    candle, excluded by evaluate_dma_state's own market-hours gate) — so without this pass every
    open card's square would sit on yesterday's date until the NEXT day's first tick recomputes it,
    one calendar day late. Run once after today's official EOD raw_prices row lands: recompute,
    and insert ONLY the rows whose data_date IS today — a symbol whose EOD row has not landed yet
    is SKIPPED and LOGGED (ENGINE_LIVENESS_RULE: never guessed forward), not silently carried.
    Read-only w.r.t. the engine — writes only v8_pivot_star_log, same as run_tick."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        d = _ist_now().date()
        with conn.cursor() as cur:
            _retired, _ = retired_baskets(cur)
            cur.execute("""
                SELECT DISTINCT ON (p.symbol) p.symbol
                FROM v8_paper_positions p
                LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
                WHERE p.status = 'OPEN'
                  AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
                  AND NOT (p.basket = ANY(%(retired)s))
                ORDER BY p.symbol, p.entry_ts DESC""", {"retired": _retired})
            syms = [r[0] for r in cur.fetchall()]
            if not syms:
                log.info("cc#1682 dma_state_eod: no open positions, nothing to evaluate")
                return {"ok": True, "date": str(d), "evaluated_today": 0, "new_rows": 0,
                        "skipped_not_landed": [], "zero_tick": True}
            cur.execute("SELECT symbol, MAX(price_date) FROM raw_prices WHERE symbol = ANY(%s) GROUP BY symbol",
                        (syms,))
            landed = {r[0]: r[1] for r in cur.fetchall()}
        not_landed = sorted(s for s in syms if landed.get(s) != d)
        if not_landed:
            log.warning("cc#1682 dma_state_eod: %d/%d symbol(s) not landed for %s, skipped: %s",
                        len(not_landed), len(syms), d, not_landed)
        # evaluate_dma_state itself resolves each symbol's OWN data_date; a symbol not landed simply
        # comes back with data_date < d and is filtered out below rather than special-cased above —
        # one code path, the not_landed log line is purely diagnostic.
        states = evaluate_dma_state(conn, d)
        todays = [x for x in states if x["data_date"] == d]
        ts = _ist_now()
        wrote = 0
        with conn.cursor() as cur:
            for x in todays:
                cur.execute("""
                    INSERT INTO v8_pivot_star_log
                      (star_date, first_seen_ts, symbol, basket, direction, star_color,
                       level_name, level_value, pp, cmp_at_star, day_1d)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, star_date, direction) DO NOTHING
                """, (x["data_date"], ts, x["symbol"], x["basket"], x["direction"], x["star_color"],
                      x["level_name"], x["level_value"], x["pp"], x["cmp_at_star"], x["day_1d"]))
                wrote += cur.rowcount
        conn.commit()
        log.info("cc#1682 dma_state_eod: %d landed-today states (%d new rows), %d symbol(s) not "
                 "landed%s", len(todays), wrote, len(not_landed),
                 " (VALID: nothing landed yet)" if not todays and not not_landed else "")
        return {"ok": True, "date": str(d), "evaluated_today": len(todays), "new_rows": wrote,
                "skipped_not_landed": not_landed,
                "zero_tick": not todays and not wrote}
    except Exception as e:
        log.exception("cc#1682 dma_state_eod failed")
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
                        # cc#1887: rows logged after this fix carry level_name='VOL_4CHECK' (the
                        # Vol R/P/D/AD tally, cc#1811) — a third era alongside the two cc#1441
                        # already documents below. Old rows are never deleted/rewritten, so all
                        # three eras must keep rendering correctly off whatever they actually hold.
                        if trig == "VOL_4CHECK" and lv is not None:
                            facts.append(f"{int(lv)} of 4 volume checks pass")
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
            # cc#1682: DMA STATE squares are the THIRD list — LOG read, not a live re-eval, same
            # cc#1008 reason as the other three lists. Supersedes the cc#1539 cross-only read: this
            # is now "latest DMA_ row per OPEN symbol", not "rows dated the page's resolved
            # star_date" — a state row's own data_date legitimately lags the resolved date during
            # market hours (evaluate_dma_state's own doc comment explains why), so reading it by
            # star_date=%s the way the other three lists do would blank every square until the
            # post-close pass ran. Scoped to the OPEN book (same candidate rule every evaluator
            # uses) so a since-closed symbol's old square never lingers.
            dma = []
            try:
                with conn.cursor() as dcur:
                    _dma_retired, _ = retired_baskets(dcur)
                    dcur.execute("""
                        WITH book AS (
                            SELECT DISTINCT ON (p.symbol) p.symbol
                            FROM v8_paper_positions p
                            LEFT JOIN app_config c ON c.key = 'v8_paper_rebuild_cutover_ts'
                            WHERE p.status = 'OPEN'
                              AND (c.value IS NULL OR p.entry_ts >= c.value::timestamp)
                              AND NOT (p.basket = ANY(%(retired)s))
                            ORDER BY p.symbol, p.entry_ts DESC
                        )
                        SELECT DISTINCT ON (g.symbol) g.symbol, g.direction, g.star_color,
                               g.level_value, g.pp, g.star_date
                        FROM v8_pivot_star_log g
                        JOIN book b ON b.symbol = g.symbol
                        WHERE g.direction IN ('DMA_ABOVE','DMA_BELOW')
                        ORDER BY g.symbol, g.star_date DESC""", {"retired": _dma_retired})
                    for sym, dirn, col, lv, ppv, sdate in dcur.fetchall():
                        rel = "above" if dirn == "DMA_ABOVE" else "below"
                        lvf, ppf = _f(lv), _f(ppv)
                        dma.append({
                            "symbol": sym, "color": col,
                            "dma5": lvf, "dma20": ppf,
                            "data_date": str(sdate) if sdate else None,
                            # FACTS ONLY — same wall as star_note().
                            "note": (f"5DMA {lvf:,.2f} {rel} 20DMA {ppf:,.2f}"
                                     + (f" (as of {sdate.strftime('%d-%b')})" if sdate else "")
                                     if lvf is not None and ppf is not None
                                     else f"5DMA {rel} 20DMA"),
                        })
            except Exception as e:
                log.warning("cc#1682 dma-state log-read failed: %s", e)
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
            # cc#1880: GREEN TRIANGLE channel-rejection markers — the fifth list, LOG read like the
            # other four (cc#1008 DISPLAY_PARITY: never a live re-eval). Written by
            # v8_channel_5m.evaluate_triangle_markers(), same v8_pivot_star_log table, direction
            # values CHAN_SELL/CHAN_BUY (cannot collide with BUY/SELL/ACTIVITY/DMA_*/TC_STRONG
            # under the table's own UNIQUE(symbol,star_date,direction)). COLUMN REUSE, stated at
            # the write site: level_value=pct move from touch to latest close, pp=touch bar's own
            # close, cmp_at_star=latest close, day_1d=n_bars, touched_dates=the touch bar's date.
            chan = []
            try:
                with conn.cursor() as ccur:
                    ccur.execute("""
                        SELECT symbol, direction, level_value, pp, cmp_at_star, day_1d, touched_dates
                        FROM v8_pivot_star_log
                        WHERE star_date=%s AND direction IN ('CHAN_SELL','CHAN_BUY')
                        ORDER BY symbol""", (d,))
                    for sym, dirn, pct, touch_close, latest_close, nbars, tdates in ccur.fetchall():
                        pctf, touchf, latf = _f(pct), _f(touch_close), _f(latest_close)
                        tdate = tdates[0] if tdates else None
                        rel_up = "below" if dirn == "CHAN_SELL" else "above"
                        chan.append({
                            "symbol": sym, "direction": dirn, "star_color": "GREEN",
                            "glyph": "triangle",
                            "pct_move": pctf, "touch_close": touchf, "latest_close": latf,
                            "n_bars": int(nbars) if nbars is not None else None,
                            "touch_date": str(tdate) if tdate else None,
                            # FACTS ONLY, same wall as every other marker note.
                            "note": (f"touched the {'upper' if dirn=='CHAN_SELL' else 'lower'} band "
                                     f"on {tdate.strftime('%d-%b')}, now {abs(pctf):.1f}% {rel_up} "
                                     f"that bar's close" if pctf is not None and tdate
                                     else f"channel band rejection ({'sell' if dirn=='CHAN_SELL' else 'buy'} side)"),
                        })
            except Exception as e:
                log.warning("cc#1880 channel_5m log-read failed: %s", e)
                chan = []
            return {
                # cc#1032: the RESOLVED date, so every surface is honest about which session it is
                # showing, plus an explicit flag rather than making a reader compare dates.
                "star_date": d, "as_of_is_last_session": (d != _today),
                "count": len(rows), "stars": rows,
                "activity": acts, "activity_count": len(acts),
                # cc#1682: dma_state is the current name; dma_cross stays as an ALIAS returning the
                # SAME list for one release so neither surface breaks mid-sprint (scope item 4).
                "dma_state": dma, "dma_state_count": len(dma),
                "dma_cross": dma, "dma_cross_count": len(dma),
                "tc_strong": tcs, "tc_strong_count": len(tcs),
                "channel_reject": chan, "channel_reject_count": len(chan),
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
                    "⚡ = Volume confirm · 2+ of 4 volume checks (pace, prior day, delivery, accumulation/distribution)",
                    "Green/red square = 5DMA above/below 20DMA",
                    "Green triangle = touched the 5-min channel band (last 3 trading days) and "
                    "reversed 1%+ off that touch",
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
