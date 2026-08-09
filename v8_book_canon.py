"""v8_book_canon.py — cc#970 · V8_PNL_CANON_V1 (CLAUDE.md rule 13, session_log 18337).

THE ONE V8 BOOK FORMULA. Every surface reads this and none of them recomputes.

WHY THIS FILE EXISTS
    On 09-Aug the founder found the site reporting DIFFERENT realised P&L on different screens.
    The Master Dashboard said 349,612 while the Trade Log tab said something else, because each
    surface carried its own SQL under its own private scope: some fresh-era, some all-era, some
    counting a win as pnl>0 and some as result='TARGET', and every one of them excluding only
    's1_reclaim_obs' by a hardcoded string while `buy_s1_bounce` — a retired basket — kept
    contributing +67,154 to every all-era figure.

    Measured at build time (2026-08-09):
        all-era                       93 trades   gross 626,408.30
        buy_s1_bounce                 10 trades   gross  67,154.10   (25-Jun .. 03-Jul)
        s1_reclaim_obs                 0 trades   gross       0.00
        fresh era, retired excluded   33 trades   gross 366,112.45
        CANONICAL realised (net)                        349,612.45
        wins 13 (TARGET) · losses 5 (SL) · decided 18 · rate 72.2%

THE FORMULA — rule 13, verbatim, in one place:
    era        entry_ts >= app_config.v8_paper_rebuild_cutover_ts
    retired    basket NOT IN (registry set) — see below
    realised   SUM(pnl) NET of Rs.500 x closed trades; gross ships alongside so the deduction
               is visible rather than silent
    wins       result = 'TARGET' only
    losses     result = 'SL' only
    rate       over DECIDED (wins + losses), never over trades — 15 of the 33 fresh trades
               closed for some other reason: real money in the realised figure, not a verdict
    dash       no decided trades => rate is None => surfaces print a dash, NEVER 0%

REGISTRY, NOT A NAME LIST (rule 9 corollary)
    The retired set comes from app_config key `v8_retired_baskets`. Retiring a future basket is
    one row and zero code. The doctrine set is NOT duplicated as a constant here — if the key is
    missing the payload says so explicitly (`registry_missing: true`) and falls back to the
    doctrine pair rather than silently letting a retired basket back into a P&L figure. A wrong
    number that looks right is the failure mode this whole file exists to prevent, so the
    fallback is loud, not quiet.

READ PATH ONLY. No engine writes, no schema change beyond that one app_config row.
"""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from fastapi import APIRouter

router = APIRouter()
log = logging.getLogger("v8_book_canon")

IST = ZoneInfo("Asia/Kolkata")
BROKERAGE_PER_TRADE = 500          # rule 13: Rs.500 per closed trade
RETIRED_KEY = "v8_retired_baskets"
CUTOVER_KEY = "v8_paper_rebuild_cutover_ts"

# Only used when the registry row is absent, and never silently — see the module docstring.
_DOCTRINE_FALLBACK = ["s1_reclaim_obs", "buy_s1_bounce"]


def _conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _f(v):
    return None if v is None else float(v)


def retired_baskets(cur):
    """The retired set, from the registry. Returns (list, missing_flag).

    Accepts a JSON array or a comma-separated string, because a founder editing a config row by
    hand should not have to remember which one it was.
    """
    cur.execute("SELECT value FROM app_config WHERE key=%s", (RETIRED_KEY,))
    row = cur.fetchone()
    raw = row[0] if row and row[0] else None
    if not raw:
        return list(_DOCTRINE_FALLBACK), True
    raw = str(raw).strip()
    names = None
    if raw.startswith("["):
        # It is MEANT to be a JSON array. If it will not parse, do NOT fall through to the
        # comma splitter: that turns "[oops" into a basket literally named "[oops", which
        # matches nothing, which silently excludes NOTHING and lets a retired basket back into
        # every P&L figure looking perfectly healthy. Treat it as missing and say so.
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                names = [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            names = None
        if not names:
            return list(_DOCTRINE_FALLBACK), True
    else:
        names = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    if not names:
        return list(_DOCTRINE_FALLBACK), True
    return names, False


def _cutover(cur):
    cur.execute("SELECT value FROM app_config WHERE key=%s", (CUTOVER_KEY,))
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def book_canon(conn, era: str = "fresh") -> dict:
    """The canonical book. `era='all'` drops the DATE bound only — retired baskets are excluded
    either way, because rule 13 says they vanish from all P&L displays completely, including
    history. An all-era payload carries era='all' so the surface can label it.
    """
    with conn.cursor() as cur:
        retired, missing = retired_baskets(cur)
        cutover = None if era == "all" else _cutover(cur)
        p = {"cut": cutover, "retired": retired}

        # ── OPEN book: unrealised, split by side, with capital deployed per side ──────────────
        cur.execute("""
            SELECT
                COUNT(*) AS open_n,
                COALESCE(SUM((COALESCE(lp.cmp, p.entry_price) - p.entry_price) * p.qty *
                    CASE WHEN UPPER(p.side) = 'SHORT' THEN -1 ELSE 1 END), 0) AS unrealised,
                COUNT(*) FILTER (WHERE UPPER(p.side) <> 'SHORT') AS n_long,
                COALESCE(SUM((COALESCE(lp.cmp, p.entry_price) - p.entry_price) * p.qty)
                    FILTER (WHERE UPPER(p.side) <> 'SHORT'), 0) AS unrl_long,
                COALESCE(SUM(p.entry_price * p.qty)
                    FILTER (WHERE UPPER(p.side) <> 'SHORT'), 0) AS dep_long,
                COUNT(*) FILTER (WHERE UPPER(p.side) = 'SHORT') AS n_short,
                COALESCE(SUM((p.entry_price - COALESCE(lp.cmp, p.entry_price)) * p.qty)
                    FILTER (WHERE UPPER(p.side) = 'SHORT'), 0) AS unrl_short,
                COALESCE(SUM(p.entry_price * p.qty)
                    FILTER (WHERE UPPER(p.side) = 'SHORT'), 0) AS dep_short
            FROM v8_paper_positions p
            LEFT JOIN LATERAL (
                SELECT close AS cmp FROM intraday_prices
                WHERE symbol = p.symbol AND source <> 'fyers_fut'
                ORDER BY ts DESC LIMIT 1
            ) lp ON true
            WHERE p.status = 'OPEN'
              AND (%(cut)s::timestamp IS NULL OR p.entry_ts >= %(cut)s::timestamp)
              AND NOT (p.basket = ANY(%(retired)s))
        """, p)
        ob = _rows(cur)[0]

        # ── CLOSED book: one pass, totals and the per-side record together so they cannot drift ──
        cur.execute("""
            SELECT COUNT(*) AS trades, COALESCE(SUM(pnl), 0) AS gross,
                   COUNT(*) FILTER (WHERE result = 'TARGET') AS wins,
                   COUNT(*) FILTER (WHERE result = 'SL') AS losses,
                   COUNT(*) FILTER (WHERE UPPER(side) <> 'SHORT') AS trades_long,
                   COUNT(*) FILTER (WHERE UPPER(side) <> 'SHORT' AND result = 'TARGET') AS wins_long,
                   COUNT(*) FILTER (WHERE UPPER(side) <> 'SHORT' AND result = 'SL') AS losses_long,
                   COUNT(*) FILTER (WHERE UPPER(side) = 'SHORT') AS trades_short,
                   COUNT(*) FILTER (WHERE UPPER(side) = 'SHORT' AND result = 'TARGET') AS wins_short,
                   COUNT(*) FILTER (WHERE UPPER(side) = 'SHORT' AND result = 'SL') AS losses_short,
                   COALESCE(SUM(entry_price * qty), 0) AS realised_deployed
            FROM v8_paper_trades
            WHERE (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND NOT (basket = ANY(%(retired)s))
        """, p)
        cb = _rows(cur)[0]

    def _side(n_key, u_key, d_key):
        n = ob[n_key] or 0
        if n == 0:
            return {"n": 0, "unrealised": None, "deployed": None, "pct": None}
        u = round(_f(ob[u_key]) or 0.0, 2)
        d = round(_f(ob[d_key]) or 0.0, 2)
        return {"n": n, "unrealised": u, "deployed": d,
                "pct": round(u / d * 100.0, 2) if d else None}

    def _record(w_key, l_key, n_key):
        w, l = cb[w_key] or 0, cb[l_key] or 0
        dec = w + l
        return {"wins": w, "losses": l, "decided": dec, "trades": cb[n_key] or 0,
                "rate": round(100.0 * w / dec, 1) if dec else None}

    side_long = _side("n_long", "unrl_long", "dep_long")
    side_short = _side("n_short", "unrl_short", "dep_short")
    dep_total = (side_long["deployed"] or 0.0) + (side_short["deployed"] or 0.0)
    unrl_total = round(_f(ob["unrealised"]) or 0.0, 2)

    trades = cb["trades"] or 0
    gross = round(_f(cb["gross"]) or 0.0, 2)
    brokerage = trades * BROKERAGE_PER_TRADE
    wins, losses = cb["wins"] or 0, cb["losses"] or 0
    decided = wins + losses
    real_dep = round(_f(cb["realised_deployed"]) or 0.0, 2)
    realised = round(gross - brokerage, 2) if trades else None

    payload = {
        "canon": "V8_PNL_CANON_V1",
        "era": era,
        "cutover": str(cutover) if cutover else None,
        "retired_baskets": retired,
        "registry_key": RETIRED_KEY,
        "registry_missing": missing,

        "open": ob["open_n"] or 0,
        "unrealised": unrl_total,
        "deployed": round(dep_total, 2) if dep_total else None,
        "unrealised_pct": (round(unrl_total / dep_total * 100.0, 2) if dep_total else None),
        "long": side_long,
        "short": side_short,

        "trades": trades,
        "gross": gross,
        "brokerage": brokerage,
        "realised": realised,
        "realised_deployed": real_dep if trades else None,
        "realised_pct": (round((gross - brokerage) / real_dep * 100.0, 2) if real_dep else None),
        "wins": wins,
        "losses": losses,
        "decided": decided,
        # dash never 0% — no verdicts is not a 0% record
        "win_rate": (round(100.0 * wins / decided, 1) if decided else None),
        "rec_long": _record("wins_long", "losses_long", "trades_long"),
        "rec_short": _record("wins_short", "losses_short", "trades_short"),

        "as_of": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if missing:
        log.warning("V8_PNL_CANON: app_config['%s'] is missing — using the doctrine fallback %s. "
                    "The payload is flagged registry_missing=true.", RETIRED_KEY, _DOCTRINE_FALLBACK)
    return payload


@router.get("/api/v8/book_canon")
def api_book_canon(era: str = "fresh"):
    """The one book endpoint. Every surface reads this; none recompute.

    era=fresh (default) — the canonical book, rule 13.
    era=all             — history. The DATE bound is dropped, retired baskets are STILL excluded,
                          and the payload says era='all' so the surface can label it as history.
    """
    era = "all" if str(era).lower() == "all" else "fresh"
    try:
        with _conn() as conn:
            return book_canon(conn, era=era)
    except Exception as e:
        log.exception("book_canon failed")
        return {"error": str(e)[:300], "canon": "V8_PNL_CANON_V1"}
