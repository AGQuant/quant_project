"""cc#1575 — OI STRUCTURE (OI_STRUCTURE_INTERPRET_V1, session_log 36283).

A daily snapshot of the option book for NIFTY and BANKNIFTY — max pain, the call wall (the
biggest call OI strike, the "ceiling"), the put wall (the biggest put OI strike, the "support
level"), chain-wide PCR — classified into one of six scenarios, plus the plain-words read the
Max Pain (i) shows on the app Home card and the web Index Intel card. ONE composer,
/api/oi/structure, serves both surfaces so the words can never differ between them.

WHAT IS COMPUTED WHERE
  * max pain          -> max_pain.max_pain() over max_pain.LATEST_CHAIN_SQL (cc#1155). The math
                         and its guard stay in max_pain.py; this file only calls them.
  * walls             -> argmax OI per side at the SAME tick, second walls = the runner-up strike.
  * one-sided         -> max_pain.one_sided() (cc#1354): a chain missing one leg gets NO read.
  * spot (live)       -> cmp_prices first, basis stated (cc#1167); previous close as the fallback
                         outside hours, basis stated.
  * spot (backfill)   -> the index SPOT bar (price_sources.INDEX_SPOT_SOURCES, never futures) at or
                         before the snapshot tick, basis 'intraday_bar'.
  * next day (T+1)    -> fill_next_day(): the next session's last close / high / low vs this
                         row's spot, from intraday_prices spot sources only.

SCENARIOS (36283) and the precedence used here, stated because the definitions overlap:
  ONE_SIDED        one leg absent this tick — no read, say the feed gap.
  PIN              call wall == put wall == max pain within one strike step AND spot within 1.5%
                   of it (the sample_pin: "most bets sit at 57,500, price is 470 below").
  ABOVE_CALL_WALL  spot above the biggest call OI strike.
  BELOW_PUT_WALL   spot below the biggest put OI strike.
  MAX_PAIN_FAR     |max pain - spot| > 1.5% with spot still inside the walls.
  RANGE            put wall < spot < call wall.

EVIDENCE is counted ONLY from oi_structure_daily rows of the same underlying and scenario whose
next_day_pct is filled (close snapshots, last 60 sessions). Below 20 such sessions the payload
says scored=false and the line reads "Only N such days on record. Too few to trust yet." — the
words stay descriptive, never a direction call (36283 out_of_scope). Nothing here is a signal.

LANGUAGE (36283 plain_words_v2): one idea per sentence, at most ten words per sentence, no
jargon — "call sellers", "put sellers", "losing money", "exit", "shift lower"; the put wall is
the "support level" and the call wall the "ceiling" in headlines. Never the word Scorr.
"""

import os
import logging
from datetime import date, datetime, timedelta, time as dt_time, timezone
from typing import Dict, List, Optional, Tuple

import psycopg
from fastapi import APIRouter

import max_pain as max_pain_mod
from price_sources import INDEX_SPOT_SOURCES

log = logging.getLogger("oi_structure")
router = APIRouter()

IST = timezone(timedelta(hours=5, minutes=30))
UNDERLYINGS = ("NIFTY", "BANKNIFTY")
SPOT_SYM = {"NIFTY": "NIFTY50", "BANKNIFTY": "BANKNIFTY"}
KIND_TIMES = {"mid": (11, 0), "close": (15, 25)}
FAR_PCT = 1.5            # MAX_PAIN_FAR threshold, 36283
PIN_STRONG_DTE = 3       # the pin "strengthens inside the last 3 sessions to expiry"
EVIDENCE_MIN = 20        # below this the read is framework-only
EVIDENCE_SESSIONS = 60
HISTORY_N = 10
BACKFILL_FROM = date(2026, 8, 26)   # option_chain full-strike history starts here (36283 data_reality)

DDL = """
CREATE TABLE IF NOT EXISTS oi_structure_daily (
    underlying        TEXT        NOT NULL,
    d                 DATE        NOT NULL,
    snapshot_kind     TEXT        NOT NULL,
    snapshot_ts       TIMESTAMP,
    expiry            DATE,
    spot              NUMERIC,
    spot_basis        TEXT,
    max_pain          NUMERIC,
    call_wall         NUMERIC,
    call_wall_oi      BIGINT,
    put_wall          NUMERIC,
    put_wall_oi       BIGINT,
    second_call_wall  NUMERIC,
    second_put_wall   NUMERIC,
    pcr               NUMERIC,
    one_sided         BOOLEAN     NOT NULL DEFAULT FALSE,
    scenario          TEXT,
    mp_dist_pct       NUMERIC,
    range_width_pct   NUMERIC,
    days_to_expiry    INTEGER,
    next_day_pct      NUMERIC,
    next_day_high_pct NUMERIC,
    next_day_low_pct  NUMERIC,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (underlying, d, snapshot_kind)
)
"""

# The chain at ONE tick of ONE expiry as it stood on a given day: the latest tick at or before
# %(t)s on %(d)s, nearest expiry as of that day. Same shape as max_pain.LATEST_CHAIN_SQL with
# the tick pinned, so the backfill measures exactly what the live job measures.
CHAIN_AT_SQL = """
    WITH exp AS (
        SELECT MIN(expiry) AS e FROM option_chain
        WHERE underlying = %(u)s AND expiry >= %(d)s
    ),
    mts AS (
        SELECT MAX(ts) AS t FROM option_chain
        WHERE underlying = %(u)s AND expiry = (SELECT e FROM exp)
          AND ts >= %(d)s::timestamp AND ts <= %(t)s
    )
    SELECT strike,
           SUM(CASE WHEN option_type = 'CE' THEN oi ELSE 0 END) AS ce,
           SUM(CASE WHEN option_type = 'PE' THEN oi ELSE 0 END) AS pe,
           (SELECT e FROM exp) AS expiry,
           (SELECT t FROM mts) AS tick
    FROM option_chain
    WHERE underlying = %(u)s
      AND expiry = (SELECT e FROM exp)
      AND ts = (SELECT t FROM mts)
      AND oi IS NOT NULL
    GROUP BY strike
    ORDER BY strike
"""


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def ensure_table(cur):
    """Idempotent. Called by the JOBS (and the backfill), never by the request handler — the
    composer only reads, and a missing table there degrades to 'no evidence yet'."""
    cur.execute(DDL)


def norm_underlying(s: Optional[str]) -> str:
    u = (s or "NIFTY").upper().strip()
    if u in ("NIFTY50", "NIFTY 50"):
        u = "NIFTY"
    if u in ("BANKNIFTY50", "NIFTYBANK", "BNF"):
        u = "BANKNIFTY"
    return u if u in UNDERLYINGS else "NIFTY"


def _f(x):
    return None if x is None else float(x)


# ── 1. STRUCTURE FROM A CHAIN ──────────────────────────────────────────────────────────────

def structure_from_rows(rows) -> Optional[dict]:
    """rows = (strike, ce_oi, pe_oi, expiry, tick) per strike at ONE tick. Returns the structure
    dict or None when there is no chain at all."""
    if not rows:
        return None
    chain: Dict[float, Tuple[float, float]] = {float(r[0]): (float(r[1] or 0), float(r[2] or 0)) for r in rows}
    expiry, tick = rows[0][3], rows[0][4]
    usable = {s: v for s, v in chain.items() if (v[0] or v[1])}
    side = max_pain_mod.one_sided(usable) if usable else None
    mp, _curve = max_pain_mod.max_pain(chain)      # the guard lives there (cc#1155 / cc#1354)

    def top2(idx):
        ordered = sorted(((s, v[idx]) for s, v in usable.items() if v[idx] > 0), key=lambda kv: (-kv[1], kv[0]))
        first = ordered[0] if ordered else None
        second = ordered[1] if len(ordered) > 1 else None
        return first, second

    (cw, cw2), (pw, pw2) = top2(0), top2(1)
    tot_ce = sum(v[0] for v in usable.values())
    tot_pe = sum(v[1] for v in usable.values())
    strikes = sorted(usable)
    step = min(b - a for a, b in zip(strikes, strikes[1:])) if len(strikes) > 1 else None
    return {
        "expiry": expiry, "tick": tick, "n_strikes": len(usable),
        "max_pain": mp,
        "call_wall": cw[0] if cw else None, "call_wall_oi": int(cw[1]) if cw else None,
        "put_wall": pw[0] if pw else None, "put_wall_oi": int(pw[1]) if pw else None,
        "second_call_wall": cw2[0] if cw2 else None, "second_put_wall": pw2[0] if pw2 else None,
        "pcr": (round(tot_pe / tot_ce, 3) if tot_ce > 0 else None),
        "one_sided": side,            # None | 'PE_MISSING' | 'CE_MISSING'
        "strike_step": step,
    }


def classify(spot: Optional[float], st: dict) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """(scenario, mp_dist_pct, range_width_pct). Precedence is documented at the top of the file."""
    mp, cw, pw = st.get("max_pain"), st.get("call_wall"), st.get("put_wall")
    if st.get("one_sided"):
        return "ONE_SIDED", None, None
    if not spot:
        return None, None, None
    mp_dist = round((mp - spot) / spot * 100, 2) if mp is not None else None
    width = round((cw - pw) / spot * 100, 2) if (cw is not None and pw is not None) else None
    tol = st.get("strike_step") or 0
    pinned = (mp is not None and cw is not None and pw is not None
              and abs(cw - pw) <= tol and abs(mp - cw) <= tol and abs(mp - pw) <= tol)
    if pinned and mp_dist is not None and abs(mp_dist) <= FAR_PCT:
        return "PIN", mp_dist, width
    if cw is not None and spot > cw:
        return "ABOVE_CALL_WALL", mp_dist, width
    if pw is not None and spot < pw:
        return "BELOW_PUT_WALL", mp_dist, width
    if mp_dist is not None and abs(mp_dist) > FAR_PCT:
        return "MAX_PAIN_FAR", mp_dist, width
    if cw is not None and pw is not None:
        return "RANGE", mp_dist, width
    return None, mp_dist, width


# ── 2. SPOT ────────────────────────────────────────────────────────────────────────────────

def spot_live(cur, underlying: str):
    """cc#1167: live cmp first, basis stated; previous close as the honest fallback."""
    sym = SPOT_SYM[underlying]
    cur.execute("SELECT cmp, updated_at FROM cmp_prices WHERE symbol=%s", (sym,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return float(r[0]), "live", (r[1].isoformat() if r[1] else None)
    cur.execute("SELECT close, price_date FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 1", (sym,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return float(r[0]), "prev_close", str(r[1])
    return None, None, None


def spot_at(cur, underlying: str, at_ts: datetime):
    """The index SPOT bar (never futures) at or before at_ts on that day; previous close if the
    day has no spot bar by then."""
    sym = SPOT_SYM[underlying]
    day0 = datetime.combine(at_ts.date(), dt_time(0, 0))
    cur.execute("""SELECT close, ts FROM intraday_prices
                   WHERE symbol=%s AND source = ANY(%s) AND ts >= %s AND ts <= %s
                   ORDER BY ts DESC LIMIT 1""", (sym, list(INDEX_SPOT_SOURCES), day0, at_ts))
    r = cur.fetchone()
    if r and r[0] is not None:
        return float(r[0]), "intraday_bar", r[1].isoformat()
    cur.execute("SELECT close, price_date FROM raw_prices WHERE symbol=%s AND price_date < %s ORDER BY price_date DESC LIMIT 1",
                (sym, at_ts.date()))
    r = cur.fetchone()
    if r and r[0] is not None:
        return float(r[0]), "prev_close", str(r[1])
    return None, None, None


# ── 3. SNAPSHOT ROWS ───────────────────────────────────────────────────────────────────────

def build_snapshot(cur, underlying: str, kind: str, d: Optional[date] = None,
                   at_ts: Optional[datetime] = None, live: bool = True) -> Optional[dict]:
    """One oi_structure_daily row (as a dict), not yet written. live=True reads the latest chain
    tick and the live spot (the scheduled jobs); live=False pins the chain to the latest tick at
    or before at_ts on day d and reads the spot bar there (the backfill)."""
    if live:
        cur.execute(max_pain_mod.LATEST_CHAIN_SQL, {"u": underlying})
        rows = cur.fetchall()
        st = structure_from_rows(rows)
        if not st:
            return None
        spot, basis, _asof = spot_live(cur, underlying)
        tick = st["tick"]
        d = (tick.date() if tick else datetime.now(IST).date())
    else:
        cur.execute(CHAIN_AT_SQL, {"u": underlying, "d": d, "t": at_ts})
        rows = cur.fetchall()
        st = structure_from_rows(rows)
        if not st or st["tick"] is None:
            return None
        spot, basis, _asof = spot_at(cur, underlying, st["tick"])
    scenario, mp_dist, width = classify(spot, st)
    expiry = st["expiry"]
    return {
        "underlying": underlying, "d": d, "snapshot_kind": kind, "snapshot_ts": st["tick"],
        "expiry": expiry, "spot": spot, "spot_basis": basis,
        "max_pain": st["max_pain"], "call_wall": st["call_wall"], "call_wall_oi": st["call_wall_oi"],
        "put_wall": st["put_wall"], "put_wall_oi": st["put_wall_oi"],
        "second_call_wall": st["second_call_wall"], "second_put_wall": st["second_put_wall"],
        "pcr": st["pcr"], "one_sided": bool(st["one_sided"]), "scenario": scenario,
        "mp_dist_pct": mp_dist, "range_width_pct": width,
        "days_to_expiry": ((expiry - d).days if (expiry and d) else None),
        "n_strikes": st["n_strikes"],
    }


UPSERT_SQL = """
    INSERT INTO oi_structure_daily
        (underlying, d, snapshot_kind, snapshot_ts, expiry, spot, spot_basis, max_pain,
         call_wall, call_wall_oi, put_wall, put_wall_oi, second_call_wall, second_put_wall,
         pcr, one_sided, scenario, mp_dist_pct, range_width_pct, days_to_expiry, computed_at)
    VALUES (%(underlying)s, %(d)s, %(snapshot_kind)s, %(snapshot_ts)s, %(expiry)s, %(spot)s, %(spot_basis)s, %(max_pain)s,
            %(call_wall)s, %(call_wall_oi)s, %(put_wall)s, %(put_wall_oi)s, %(second_call_wall)s, %(second_put_wall)s,
            %(pcr)s, %(one_sided)s, %(scenario)s, %(mp_dist_pct)s, %(range_width_pct)s, %(days_to_expiry)s, NOW())
    ON CONFLICT (underlying, d, snapshot_kind) DO UPDATE SET
        snapshot_ts = EXCLUDED.snapshot_ts, expiry = EXCLUDED.expiry, spot = EXCLUDED.spot,
        spot_basis = EXCLUDED.spot_basis, max_pain = EXCLUDED.max_pain,
        call_wall = EXCLUDED.call_wall, call_wall_oi = EXCLUDED.call_wall_oi,
        put_wall = EXCLUDED.put_wall, put_wall_oi = EXCLUDED.put_wall_oi,
        second_call_wall = EXCLUDED.second_call_wall, second_put_wall = EXCLUDED.second_put_wall,
        pcr = EXCLUDED.pcr, one_sided = EXCLUDED.one_sided, scenario = EXCLUDED.scenario,
        mp_dist_pct = EXCLUDED.mp_dist_pct, range_width_pct = EXCLUDED.range_width_pct,
        days_to_expiry = EXCLUDED.days_to_expiry, computed_at = NOW()
"""


def _brief(row: dict) -> str:
    return "%s %s %s spot=%s(%s) mp=%s cw=%s pw=%s pcr=%s %s" % (
        row["underlying"], row["d"], row["snapshot_kind"], row["spot"], row["spot_basis"],
        row["max_pain"], row["call_wall"], row["put_wall"], row["pcr"],
        row["scenario"] or ("ONE_SIDED" if row["one_sided"] else "NO_READ"))


def run_snapshot(kind: str) -> dict:
    """The scheduled job body (11:00 mid / 15:25 close). Upserts one row per underlying from the
    LATEST chain tick; idempotent, so a catch-up re-run only refreshes the same row."""
    written, briefs = 0, []
    with _conn() as conn, conn.cursor() as cur:
        ensure_table(cur)
        for u in UNDERLYINGS:
            row = build_snapshot(cur, u, kind, live=True)
            if not row:
                briefs.append("%s: no chain tick" % u)
                continue
            cur.execute(UPSERT_SQL, row)
            written += 1
            briefs.append(_brief(row))
        conn.commit()
    log.info("oi_structure %s: %s", kind, " | ".join(briefs))
    return {"kind": kind, "written": written, "rows": briefs}


# ── 4. T+1 FILL ────────────────────────────────────────────────────────────────────────────

def fill_next_day(cur, now: Optional[datetime] = None) -> int:
    """next_day_pct / high / low for every row still unfilled whose NEXT session is complete —
    the session's last spot close, high and low vs the row's own spot. Spot sources only."""
    now = now or datetime.now(IST)
    today = now.date()
    cur.execute("""SELECT underlying, d, snapshot_kind, spot FROM oi_structure_daily
                   WHERE next_day_pct IS NULL AND spot IS NOT NULL AND d < %s ORDER BY d, underlying""", (today,))
    todo = cur.fetchall()
    filled = 0
    srcs = list(INDEX_SPOT_SOURCES)
    for u, d, kind, spot in todo:
        sym = SPOT_SYM[u]
        day_after = datetime.combine(d, dt_time(0, 0)) + timedelta(days=1)
        cur.execute("""SELECT ts FROM intraday_prices WHERE symbol=%s AND source = ANY(%s) AND ts >= %s
                       ORDER BY ts LIMIT 1""", (sym, srcs, day_after))
        r = cur.fetchone()
        if not r:
            continue
        nd = r[0].date()
        if nd > today or (nd == today and now.time() < dt_time(15, 31)):
            continue                       # the next session is not complete yet
        nd0 = datetime.combine(nd, dt_time(0, 0))
        nd1 = nd0 + timedelta(days=1)
        cur.execute("""SELECT MAX(high), MIN(low) FROM intraday_prices
                       WHERE symbol=%s AND source = ANY(%s) AND ts >= %s AND ts < %s""", (sym, srcs, nd0, nd1))
        hi, lo = cur.fetchone()
        cur.execute("""SELECT close FROM intraday_prices
                       WHERE symbol=%s AND source = ANY(%s) AND ts >= %s AND ts < %s
                       ORDER BY ts DESC LIMIT 1""", (sym, srcs, nd0, nd1))
        c = cur.fetchone()
        if not c or c[0] is None or not spot:
            continue
        s = float(spot)
        pct = round((float(c[0]) - s) / s * 100, 3)
        hp = round((float(hi) - s) / s * 100, 3) if hi is not None else None
        lp = round((float(lo) - s) / s * 100, 3) if lo is not None else None
        cur.execute("""UPDATE oi_structure_daily SET next_day_pct=%s, next_day_high_pct=%s, next_day_low_pct=%s
                       WHERE underlying=%s AND d=%s AND snapshot_kind=%s""", (pct, hp, lp, u, d, kind))
        filled += 1
    return filled


def run_fill() -> dict:
    with _conn() as conn, conn.cursor() as cur:
        ensure_table(cur)
        n = fill_next_day(cur)
        conn.commit()
    log.info("oi_structure fill_next_day: %d rows", n)
    return {"filled": n}


# ── 5. BACKFILL ────────────────────────────────────────────────────────────────────────────

def backfill(from_d: date = BACKFILL_FROM, to_d: Optional[date] = None) -> dict:
    """Both kinds for every option_chain day from from_d, pinned to the 11:00 / 15:25 ticks (the
    latest tick at or before each), then the T+1 fill. Re-runnable; rows are upserted."""
    to_d = to_d or datetime.now(IST).date()
    written, briefs = 0, []
    with _conn() as conn, conn.cursor() as cur:
        ensure_table(cur)
        cur.execute("SELECT DISTINCT ts::date FROM option_chain WHERE ts >= %s ORDER BY 1",
                    (datetime.combine(from_d, dt_time(0, 0)),))
        days = [r[0] for r in cur.fetchall() if r[0] <= to_d]
        for d in days:
            for u in UNDERLYINGS:
                for kind, (hh, mm) in KIND_TIMES.items():
                    row = build_snapshot(cur, u, kind, d=d, at_ts=datetime.combine(d, dt_time(hh, mm)), live=False)
                    if not row:
                        briefs.append("%s %s %s: no chain tick" % (u, d, kind))
                        continue
                    cur.execute(UPSERT_SQL, row)
                    written += 1
                    briefs.append(_brief(row))
        conn.commit()
        filled = fill_next_day(cur)
        conn.commit()
    return {"from": str(from_d), "to": str(to_d), "days": len(days), "written": written,
            "filled": filled, "rows": briefs}


# ── 6. WORDS ───────────────────────────────────────────────────────────────────────────────

def _n(x) -> str:
    return "—" if x is None else "{:,.0f}".format(float(x))


def words(scenario: Optional[str], ctx: dict) -> dict:
    """headline (<= 12 words), read (2-4 short sentences), caveats — 36283 plain_words_v2:
    one idea per sentence, max ten words per sentence, no jargon."""
    spot, mp, cw, pw = ctx.get("spot"), ctx.get("max_pain"), ctx.get("call_wall"), ctx.get("put_wall")
    dte = ctx.get("days_to_expiry")
    width, mp_dist = ctx.get("range_width_pct"), ctx.get("mp_dist_pct")
    dte_line = ("Expiry is %d days away." % dte) if dte is not None else "Expiry date is not known."
    if scenario == "ONE_SIDED":
        side = ctx.get("one_sided_leg") or ""
        missing = "put" if side == "PE_MISSING" else ("call" if side == "CE_MISSING" else "one")
        return {
            "headline": "Half the option data is missing this tick.",
            "read": ["The %s side did not arrive." % missing,
                     "No read is possible without both sides.",
                     "Check again on the next tick."],
            "caveats": ["One side of the chain was missing."],
        }
    if scenario is None or not spot:
        return {
            "headline": "No live price this tick.",
            "read": ["The option book is here but price is not.", "Check again on the next tick."],
            "caveats": [dte_line],
        }
    diff = (mp - spot) if mp is not None else None
    if scenario == "PIN":
        where = ("Price is right there." if diff is not None and abs(diff) < 1
                 else "Price is %s %s." % (_n(abs(diff)), "below" if diff > 0 else "above"))
        strong = (dte is not None and dte <= PIN_STRONG_DTE)
        return {
            "headline": "Most bets sit at %s. %s" % (_n(mp), where),
            "read": ["Big call sellers and big put sellers both picked %s." % _n(mp),
                     "They make money if expiry ends there.",
                     "Price often drifts back to such a level.",
                     dte_line,
                     "So the pull is strong now." if strong else "So the pull is weak for now."],
            "caveats": ([] if strong else ["The pull is weak until the last three sessions."]) + ["Walls can move within the day."],
        }
    if scenario == "ABOVE_CALL_WALL":
        return {
            "headline": "Price is above the ceiling at %s." % _n(cw),
            "read": ["The biggest call sellers are losing money now.",
                     "They may exit and push price higher.",
                     "Or they may shift the ceiling higher.",
                     "Watch the call count at %s." % _n(cw)]
                    + (["Most bets sit at %s." % _n(mp)] if mp is not None else []),
            "caveats": [dte_line, "Walls can move within the day."],
        }
    if scenario == "BELOW_PUT_WALL":
        return {
            "headline": "Price is below the support level at %s." % _n(pw),
            "read": ["The biggest put sellers are losing money now.",
                     "They may exit and push price lower.",
                     "Or they may shift the support lower.",
                     "Watch the put count at %s." % _n(pw)]
                    + (["Most bets sit at %s." % _n(mp)] if mp is not None else []),
            "caveats": [dte_line, "Walls can move within the day."],
        }
    if scenario == "MAX_PAIN_FAR":
        return {
            "headline": "Most bets sit at %s. Price is %.1f%% away." % (_n(mp), abs(mp_dist or 0)),
            "read": ["The option book has not caught up with price.",
                     "Max pain pulls less when it is this far."]
                    + (["It pulls less this early in the series too."] if (dte is not None and dte > PIN_STRONG_DTE) else [])
                    + [dte_line],
            "caveats": ["Walls can move within the day."],
        }
    # RANGE
    pos = ((spot - pw) / spot * 100) if (pw is not None and spot) else None
    return {
        "headline": "Price sits between support %s and ceiling %s." % (_n(pw), _n(cw)),
        "read": ["Put sellers guard %s." % _n(pw),
                 "Call sellers guard %s." % _n(cw),
                 "The band is %.1f%% wide." % (width or 0),
                 "Price is %.1f%% above the support level." % (pos or 0),
                 "Near an edge, sellers start to hedge."]
                + (["Most bets sit at %s." % _n(mp)] if mp is not None else []),
        "caveats": [dte_line, "Walls can move within the day."],
    }


def evidence_line(n: int, up: int, down: int) -> str:
    if n < EVIDENCE_MIN:
        return "Only %d such day%s on record. Too few to trust yet." % (n, "" if n == 1 else "s")
    return "%d such days on record: next day up %d, down %d." % (n, up, down)


# ── 7. COMPOSER ────────────────────────────────────────────────────────────────────────────

def _evidence(cur, underlying: str, scenario: Optional[str]) -> dict:
    out = {"n": 0, "up": 0, "down": 0, "avg_next_pct": None, "scored": False, "sessions_window": EVIDENCE_SESSIONS}
    if not scenario:
        return out
    try:
        cur.execute("""SELECT next_day_pct FROM oi_structure_daily
                       WHERE underlying=%s AND scenario=%s AND snapshot_kind='close' AND next_day_pct IS NOT NULL
                       ORDER BY d DESC LIMIT %s""", (underlying, scenario, EVIDENCE_SESSIONS))
        vals = [float(r[0]) for r in cur.fetchall()]
    except Exception as e:                       # table not there yet: no evidence, said plainly
        log.warning("oi_structure evidence unavailable: %s", e)
        return out
    n = len(vals)
    out.update({"n": n, "up": sum(1 for v in vals if v > 0), "down": sum(1 for v in vals if v < 0),
                "avg_next_pct": (round(sum(vals) / n, 2) if n else None), "scored": n >= EVIDENCE_MIN})
    return out


def _history(cur, underlying: str) -> List[dict]:
    try:
        cur.execute("""SELECT d, snapshot_kind, scenario, spot, max_pain, call_wall, put_wall, mp_dist_pct,
                              next_day_pct, one_sided
                       FROM oi_structure_daily WHERE underlying=%s AND snapshot_kind='close'
                       ORDER BY d DESC LIMIT %s""", (underlying, HISTORY_N))
        return [{"d": str(r[0]), "kind": r[1], "scenario": r[2], "spot": _f(r[3]), "max_pain": _f(r[4]),
                 "call_wall": _f(r[5]), "put_wall": _f(r[6]), "mp_dist_pct": _f(r[7]),
                 "next_day_pct": _f(r[8]), "one_sided": bool(r[9])} for r in cur.fetchall()]
    except Exception as e:
        log.warning("oi_structure history unavailable: %s", e)
        return []


@router.get("/api/oi/structure")
def oi_structure(underlying: str = "NIFTY"):
    """The ONE composer both Max Pain (i) surfaces read. Live: the latest chain tick + live spot,
    classified now; evidence and history from oi_structure_daily. Read-only, no DDL here."""
    u = norm_underlying(underlying)
    now = datetime.now(IST)
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(max_pain_mod.LATEST_CHAIN_SQL, {"u": u})
            rows = cur.fetchall()
            st = structure_from_rows(rows)
            if not st:
                return {"status": "no_data", "underlying": u, "as_of": None,
                        "note": "Option chain data pending", "evidence": _evidence(cur, u, None), "history": []}
            spot, basis, spot_asof = spot_live(cur, u)
            scenario, mp_dist, width = classify(spot, st)
            expiry = st["expiry"]
            dte = (expiry - now.date()).days if expiry else None
            ev = _evidence(cur, u, scenario)
            hist = _history(cur, u)
    except Exception as e:
        log.warning("oi_structure composer failed for %s: %s", u, e)
        return {"status": "error", "underlying": u, "note": "OI structure unavailable this tick", "as_of": None}
    ctx = {"spot": spot, "max_pain": st["max_pain"], "call_wall": st["call_wall"], "put_wall": st["put_wall"],
           "days_to_expiry": dte, "range_width_pct": width, "mp_dist_pct": mp_dist, "one_sided_leg": st["one_sided"]}
    w = words(scenario, ctx)
    line = evidence_line(ev["n"], ev["up"], ev["down"])
    tick = st["tick"]
    return {
        "status": "ok", "underlying": u, "as_of": (tick.isoformat() if tick else None),
        "spot": spot, "spot_basis": basis, "spot_asof": spot_asof,
        "expiry": (str(expiry) if expiry else None), "days_to_expiry": dte,
        "max_pain": st["max_pain"], "call_wall": st["call_wall"], "call_wall_oi": st["call_wall_oi"],
        "put_wall": st["put_wall"], "put_wall_oi": st["put_wall_oi"],
        "second_call_wall": st["second_call_wall"], "second_put_wall": st["second_put_wall"],
        "pcr": st["pcr"], "one_sided": st["one_sided"], "strikes": st["n_strikes"],
        "mp_dist_pct": mp_dist, "range_width_pct": width,
        "scenario": scenario or ("ONE_SIDED" if st["one_sided"] else None),
        "headline": w["headline"], "read": w["read"], "read_text": " ".join(w["read"]),
        "caveats": w["caveats"],
        "evidence": ev, "evidence_line": line,
        "history": hist,
        "framework_only": not ev["scored"],
        "note": "Descriptive read only. Not a trading signal.",
    }
