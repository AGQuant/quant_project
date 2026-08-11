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
#   BUY  (glyph = star, blue)
#     1 TOUCH     in the last 3 closed sessions, session low <= THAT session's own S1
#     2 POSITION  cmp between S1 and S1*1.02  OR  cmp > PP
#     3 STABILITY day_1d between -1 and +2  AND  mom_2d between -1 and +2
#   SELL (glyph = circle, red) — mirrored on R1
#     1 TOUCH     session high >= that session's own R1
#     2 POSITION  cmp between R1*0.98 and R1  OR  cmp < PP
#     3 STABILITY day_1d between -2 and +1  AND  mom_2d between -2 and +1
# V1's "up on the day" and "above 50-DMA" clauses are GONE — the stability band replaces them.
# dma_50 is still read and still logged, because the column exists and dropping the record would
# lose history for the 4-6 week review; it is simply no longer a condition.
STAB_BUY  = (-1.0, 2.0)     # day_1d and mom_2d must BOTH sit inside this, buy side
STAB_SELL = (-2.0, 1.0)     # mirrored band, sell side

# GLYPH IS PART OF THE SPEC, not a template choice: buy = star, sell = CIRCLE, never a star on the
# sell side. Served from here so web and mobile cannot disagree about it (DISPLAY_PARITY 16202).
GLYPH = {"BUY": "star", "SELL": "circle"}

# ── cc#933 GREEN_STAR_ACTIVITY_V1 — founder-locked in session_log 18053 ───────────────────────
# Same open-positions scope as V2. Fires when EITHER leg trips:
#   (a) vol_ratio > 1.5   — latest v8_metrics, day volume vs its 21-day average
#   (b) |OI day-over-day| > 25%  — futures_basis, last tick of the day vs last tick of the prior
#       session. 25% is deliberately RARE: typical DoD is 1-5%, so this leg fires only on a true
#       event or a rollover, and it is expected to be silent most days.
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
ACTIVITY_VOL_X       = 1.5
ACTIVITY_OI_DOD_PCT  = 25.0
GLYPH_SIDE = {"LONG": "star", "SHORT": "circle"}

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

        cur.execute("""SELECT DISTINCT ON (symbol) symbol, vol_ratio FROM v8_metrics
                       WHERE symbol = ANY(%s) AND score_date <= %s
                       ORDER BY symbol, score_date DESC""", (syms, d))
        volx = {r[0]: _f(r[1]) for r in cur.fetchall()}

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
        vx, od = volx.get(sym), oidod.get(sym)
        vol_hit = vx is not None and vx > ACTIVITY_VOL_X
        oi_hit = od is not None and abs(od) > ACTIVITY_OI_DOD_PCT
        if not (vol_hit or oi_hit):
            continue
        facts = []
        if vol_hit:
            facts.append(f"volume {vx:.1f}x its 21-day average")
        if oi_hit:
            facts.append(f"OI {od:+.0f}% day-over-day")
        out.append({
            "symbol": sym, "side": side,
            "star_color": "GREEN",
            "glyph": GLYPH_SIDE.get(side, "star"),
            "vol_ratio": round(vx, 2) if vx is not None else None,
            "oi_dod_pct": round(od, 2) if od is not None else None,
            "trigger": "VOL" if vol_hit and not oi_hit else ("OI" if oi_hit and not vol_hit else "VOL+OI"),
            # FACTS ONLY, same wall as star_note(): no buy/sell/entry/target wording.
            "note": " · ".join(facts),
        })
    return out


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
                      a["vol_ratio"] if a["trigger"] != "OI" else a["oi_dod_pct"]))
                awrote += cur.rowcount
        conn.commit()
        log.info("cc#856/933 pivot_star tick: %d pivot markers (%d new), %d activity (%d new)%s",
                 len(stars), wrote, len(acts), awrote,
                 " (VALID ZERO-MARKER TICK)" if not stars and not acts else "")
        return {"ok": True, "date": str(d), "evaluated": len(stars), "new_rows": wrote,
                "activity": len(acts), "activity_new_rows": awrote,
                "zero_star_tick": not stars and not acts, "scope": EVAL_SCOPE}
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
    """Today's starred symbols. Read-only; never triggers a write."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            d = star_date or str(_ist_now().date())
            # cc#1008: `stars` is the PIVOT list (BLUE/RED) only. GREEN activity rows live in the
            # same table but render via the `activity` list below — leaving them in `stars` made
            # pivotStar/pivotMark (which map star_color as BLUE?blue:red) paint every green marker
            # RED on both surfaces (the founder's GRASIM). Filtering them here fixes both copies at
            # the one shared source, with no frontend colour change.
            cur.execute("""
                SELECT symbol, basket, direction, star_color, level_name, level_value,
                       pct_from_level, day_1d, near_pp, cmp_at_star, first_seen_ts, touched_dates
                FROM v8_pivot_star_log WHERE star_date=%s AND star_color IN ('BLUE','RED')
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
            # evaluate_activity() (untouched, locked 18053) — but a live re-eval at render time can
            # fade intra-day and drop a marker the log still holds, so the surface would disagree
            # with v8_pivot_star_log. Reading the log makes what BOTH surfaces render match the table
            # exactly (the founder's own verify + DISPLAY_PARITY 16202). Glyph follows the CURRENT
            # open-position side (star long, circle short); note is rebuilt facts-only from the
            # logged trigger + value, same wall as star_note (no buy/sell/entry/target wording).
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
                        WHERE g.star_date=%s AND g.star_color='GREEN'
                        ORDER BY g.symbol""", (d,))
                    for sym, lname, lval, side in acur.fetchall():
                        side_u = (side or "").upper()
                        lv = _f(lval)
                        trig = (lname or "").upper()
                        facts = []
                        if trig in ("VOL", "VOL+OI") and lv is not None:
                            facts.append(f"volume {lv:.1f}x its 21-day average")
                        if trig == "OI" and lv is not None:
                            facts.append(f"OI {lv:+.0f}% day-over-day")
                        elif trig == "VOL+OI":
                            facts.append("OI event day-over-day")
                        acts.append({
                            "symbol": sym, "side": side_u or None,
                            "star_color": "GREEN", "color": "#2fd48b",
                            "glyph": GLYPH_SIDE.get(side_u, "star"),
                            "level_name": lname, "level_value": lv,
                            "note": " · ".join(facts) if facts else "unusual activity",
                        })
            except Exception as e:
                log.warning("cc#1008 activity log-read failed: %s", e)
                acts = []
            return {
                "star_date": d, "count": len(rows), "stars": rows,
                "activity": acts, "activity_count": len(acts),
                "scope": EVAL_SCOPE,
                "rule": ("PIVOT_STAR_V2 (session_log 18052), evaluated on the OPEN paper book. "
                         "STAR (blue, buy side): touched its own S1 in the last 3 closed sessions; "
                         "cmp within 2% above S1 or above PP; day_1d and mom_2d both between -1 "
                         "and +2. CIRCLE (red, sell side): mirrored on R1 — touched R1, cmp within "
                         "2% below R1 or below PP, day_1d and mom_2d both between -2 and +1. The "
                         "glyph is never a star on the sell side."),
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
                "legend": [
                    "Amber = Trade Check · filled STRONG, outline VALID",
                    "Blue = held reversal at S1 · Red = mirror at R1",
                    "Green = unusual activity · volume >1.5x or OI >25% day-over-day",
                    "Shape = side · star long, circle short. Colour = meaning.",
                ],
            }
    except Exception as e:
        log.exception("pivot_star endpoint failed")
        return {"star_date": star_date, "stars": [], "count": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.post("/api/v8/pivot_star/run")
def pivot_star_run():
    """Manual tick (ops/verification). The scheduled 5-min job calls run_tick() directly."""
    return run_tick()
