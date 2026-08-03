"""
global_heatstrip.py — cc#842 GLOBAL HEAT STRIP V2.

Spec source of truth: session_log id=14406 (spec_locked, GLOBAL_HEATSTRIP_V2, founder 03-Aug 02:00).

HARD CONSTRAINTS (14406): READ-ONLY. No new tables, no new fetch jobs, no new scheduler entries.
Pure render off global_indices + global_intraday + the existing India VIX path.

TWO OPEN ITEMS THE SPEC ASKED ME TO VERIFY BEFORE WIRING — both answered against the live DB:

  (1) 52-WEEK METRICS FOR ALL 17: yes. Every symbol carries 5 years of daily history in
      global_indices (1,241-1,871 rows) with 241+ rows inside the last year. The five EOD symbols
      spot-checked specifically: Shanghai 1,241 rows / 241 in 1y, ^FTSE 1,292 / 252, ^GDAXI
      1,305 / 252, INDIAVIX 1,271 / 243, ^VIX 1,286 / 252. All 52w metrics are computable.

  (2) INDIA VIX BRIDGE — THE SPEC'S TABLE NAME IS WRONG. There is no `v8_indiavix_intraday` table
      anywhere in the schema or the code. The real live path, as used by v10_endpoints.v10_vix, is
      `intraday_prices WHERE symbol='INDIAVIX'` (fed by the worker's INDEX_LTP_SYMBOLS map,
      worker/fyers_feed.py: 'INDIAVIX' -> 'NSE:INDIAVIX-INDEX') plus `cmp_prices` for the live LTP.
      This module bridges to THAT, exactly as the spec intended — no new fetch.

WHY THE PER-TILE AS-OF STAMP IS LOAD-BEARING, NOT DECORATION. Quote dates genuinely diverge across
these markets: on 03-Aug the commodities/FX/crypto rows carry 2026-08-03, the US rows 2026-08-01,
and Shanghai/FTSE/DAX/Nikkei/India VIX 2026-07-31. A strip that prints one timestamp for all of them
is asserting something false. Every tile carries its own.

WHY THE WEEK LEG USES EACH SYMBOL'S OWN SESSION CALENDAR (founder-locked). "5 trading sessions ago"
is computed per symbol from that symbol's own rows, never from a shared calendar. Shanghai, FTSE and
NSE keep different holidays; a shared calendar would silently compare across a different span for
each market and mis-date the week change.
"""

import os
import logging
from typing import Dict, Any, List, Optional

import psycopg2
from fastapi import APIRouter

log = logging.getLogger("scorr.heatstrip")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")

WEEK_SESSIONS = 5

# The 5 symbols with no global_intraday coverage. Founder-locked: they render in their OWN EOD
# BLOCK — visually distinct, each with its own as-of date. NOT dimmed into the day row, NOT excluded.
EOD_ONLY = {"000001.SS", "^FTSE", "^GDAXI", "INDIAVIX", "^VIX"}

# Inverted tiles: for volatility, UP is RISK-OFF. The inversion applies to BOTH the day and week
# legs (founder-locked) — a green VIX week would say the opposite of what happened.
INVERTED = {"^VIX", "INDIAVIX"}

CATEGORY_ORDER = ["index", "volatility", "commodity", "currency", "crypto"]
CATEGORY_LABEL = {"index": "Indices", "volatility": "Volatility", "commodity": "Commodities",
                  "currency": "Currency", "crypto": "Crypto"}


def _conn():
    return psycopg2.connect(_DB)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct(now_v, base_v) -> Optional[float]:
    if now_v is None or base_v in (None, 0):
        return None
    return round((float(now_v) / float(base_v) - 1) * 100.0, 2)


def _band(chg: Optional[float], inverted: bool) -> str:
    """Colour band. Inversion flips the SIGN, not the thresholds, so a +2% VIX day lands in the
    same deep bucket a -2% equity day does."""
    if chg is None:
        return "none"
    v = -chg if inverted else chg
    if v == 0:
        return "flat"
    if v >= 1.5:
        return "up-strong"
    if v > 0:
        return "up"
    if v <= -1.5:
        return "down-strong"
    return "down"


def build_strip(cur) -> Dict[str, Any]:
    # ── latest daily row per symbol, plus the close 5 of ITS OWN sessions back ────────────────
    cur.execute("""
        WITH ranked AS (
            SELECT symbol, name, category, price, prev_close, chg_pct, quote_date,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY quote_date DESC) rn
            FROM global_indices WHERE price IS NOT NULL
        )
        SELECT symbol, name, category, price, prev_close, chg_pct, quote_date, rn
        FROM ranked WHERE rn <= %s ORDER BY symbol, rn
    """, (WEEK_SESSIONS + 1,))
    per_sym: Dict[str, List[tuple]] = {}
    for r in cur.fetchall():
        per_sym.setdefault(r[0], []).append(r)

    # ── live intraday last tick for the 12 covered symbols ───────────────────────────────────
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, price, ts
                   FROM global_intraday WHERE price IS NOT NULL
                   ORDER BY symbol, ts DESC""")
    live = {r[0]: (_f(r[1]), r[2]) for r in cur.fetchall()}

    # ── India VIX bridge: the REAL path (see module docstring), never a new fetch ─────────────
    vix_live, vix_ts = None, None
    try:
        cur.execute("""SELECT close, ts FROM intraday_prices
                       WHERE symbol='INDIAVIX' AND close IS NOT NULL
                       ORDER BY ts DESC LIMIT 1""")
        r = cur.fetchone()
        if r:
            vix_live, vix_ts = _f(r[0]), r[1]
        cur.execute("SELECT cmp FROM cmp_prices WHERE symbol='INDIAVIX'")
        c = cur.fetchone()
        if c and c[0] is not None:
            vix_live = _f(c[0])
    except Exception as e:
        log.warning("cc#842 India VIX bridge unavailable: %s", e)

    tiles = []
    for sym, rows in per_sym.items():
        head = rows[0]
        name, cat = head[1], head[2]
        daily_px, prev_close, stored_chg, qdate = _f(head[3]), _f(head[4]), _f(head[5]), head[6]

        lp, lts = live.get(sym, (None, None))
        if sym == "INDIAVIX" and vix_live is not None:
            lp, lts = vix_live, vix_ts

        # DAY = latest tick vs prev_close. Falls back to the stored daily chg_pct when there is no
        # tick — which is exactly the case for the five EOD symbols, and why they get their own
        # block rather than sitting in the day row pretending to be live.
        is_live = lp is not None and sym not in EOD_ONLY or (sym == "INDIAVIX" and lp is not None)
        price = lp if lp is not None else daily_px
        day_chg = _pct(lp, prev_close) if (lp is not None and prev_close) else stored_chg

        # WEEK = latest close vs the close 5 of THIS symbol's own sessions back. If a symbol has
        # fewer than 6 stored sessions the answer is None, never a shorter window silently relabelled.
        week_chg, week_base_date = None, None
        if len(rows) >= WEEK_SESSIONS + 1:
            base = rows[WEEK_SESSIONS]
            week_chg = _pct(price if price is not None else daily_px, _f(base[3]))
            week_base_date = base[6]

        inv = sym in INVERTED
        tiles.append({
            "symbol": sym, "name": name, "category": cat,
            "price": price,
            "day_chg_pct": day_chg, "day_band": _band(day_chg, inv),
            "week_chg_pct": week_chg, "week_band": _band(week_chg, inv),
            "week_base_date": str(week_base_date) if week_base_date else None,
            "week_sessions": WEEK_SESSIONS,
            "as_of": str(lts) if (lts and is_live) else str(qdate),
            "as_of_is_live": bool(lts and is_live),
            "eod_only": sym in EOD_ONLY,
            "inverted": inv,
            "sessions_available": len(rows),
        })

    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    tiles.sort(key=lambda t: (order.get(t["category"], 99), t["name"] or t["symbol"]))
    return {
        "tiles": [t for t in tiles if not t["eod_only"]],
        "eod_tiles": [t for t in tiles if t["eod_only"]],
        "category_order": CATEGORY_ORDER, "category_label": CATEGORY_LABEL,
        "week_basis": f"rolling {WEEK_SESSIONS} sessions, per symbol's own session calendar",
        "inverted_symbols": sorted(INVERTED),
        "count": len(tiles),
    }


def build_detail(cur, symbol: str) -> Dict[str, Any]:
    """Click-drawer payload: a price series plus the return block, all from daily history."""
    cur.execute("""SELECT name, category FROM global_indices WHERE symbol=%s
                   ORDER BY quote_date DESC LIMIT 1""", (symbol,))
    head = cur.fetchone()
    if not head:
        return {"error": f"unknown symbol: {symbol}"}
    name, cat = head

    cur.execute("""SELECT quote_date, price FROM global_indices
                   WHERE symbol=%s AND price IS NOT NULL
                   ORDER BY quote_date""", (symbol,))
    hist = [(r[0], float(r[1])) for r in cur.fetchall()]
    if not hist:
        return {"error": f"no daily history for {symbol}"}

    last_d, last_p = hist[-1]

    def _back(n_sessions):
        i = len(hist) - 1 - n_sessions
        return hist[i][1] if i >= 0 else None

    def _on_or_before(target):
        prev = None
        for d, p in hist:
            if d <= target:
                prev = p
            else:
                break
        return prev

    from datetime import date as _date
    jan1 = _date(last_d.year, 1, 1)

    rets = {
        "1D": _pct(last_p, _back(1)),
        "1W": _pct(last_p, _back(WEEK_SESSIONS)),
        "1M": _pct(last_p, _on_or_before(last_d.replace(day=1)) if last_d.day == 1
                   else _on_or_before(_date(last_d.year if last_d.month > 1 else last_d.year - 1,
                                            last_d.month - 1 if last_d.month > 1 else 12, min(last_d.day, 28)))),
        "3M": _pct(last_p, _on_or_before(_date(last_d.year - (1 if last_d.month <= 3 else 0),
                                               (last_d.month - 3 - 1) % 12 + 1, min(last_d.day, 28)))),
        "6M": _pct(last_p, _on_or_before(_date(last_d.year - (1 if last_d.month <= 6 else 0),
                                               (last_d.month - 6 - 1) % 12 + 1, min(last_d.day, 28)))),
        "YTD": _pct(last_p, _on_or_before(jan1)),
        "1Y": _pct(last_p, _on_or_before(_date(last_d.year - 1, last_d.month, min(last_d.day, 28)))),
    }

    yr = [p for d, p in hist if (last_d - d).days <= 365]
    hi52, lo52 = (max(yr), min(yr)) if yr else (None, None)

    # The drawer's chart: intraday for the covered symbols, daily-only for the five EOD ones —
    # the spec is explicit that these are different series, not one with a gap.
    series, series_kind = [], "daily"
    if symbol not in EOD_ONLY:
        cur.execute("""SELECT ts, price FROM global_intraday
                       WHERE symbol=%s AND price IS NOT NULL ORDER BY ts""", (symbol,))
        series = [{"t": str(r[0]), "p": float(r[1])} for r in cur.fetchall()]
        if series:
            series_kind = "intraday"
    if symbol == "INDIAVIX":
        try:
            cur.execute("""SELECT ts, close FROM intraday_prices
                           WHERE symbol='INDIAVIX' AND close IS NOT NULL
                             AND ts >= NOW() - INTERVAL '8 days' ORDER BY ts""")
            s = [{"t": str(r[0]), "p": float(r[1])} for r in cur.fetchall()]
            if s:
                series, series_kind = s, "intraday"
        except Exception as e:
            log.warning("cc#842 INDIAVIX drawer series: %s", e)
    if not series:
        series = [{"t": str(d), "p": p} for d, p in hist[-260:]]
        series_kind = "daily"

    return {
        "symbol": symbol, "name": name, "category": cat,
        "price": last_p, "as_of": str(last_d),
        "returns": rets,
        "high_52w": hi52, "low_52w": lo52,
        "from_52w_high_pct": _pct(last_p, hi52),
        "sessions_in_52w": len(yr),
        "series": series, "series_kind": series_kind,
        "inverted": symbol in INVERTED,
        "eod_only": symbol in EOD_ONLY,
        "basis": "global_indices daily history" + (" + global_intraday" if series_kind == "intraday" else ""),
    }


@router.get("/api/global/heatstrip")
def heatstrip():
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_strip(cur)
    except Exception as e:
        log.exception("heatstrip failed")
        return {"tiles": [], "eod_tiles": [], "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.get("/api/global/heatstrip/{symbol:path}")
def heatstrip_detail(symbol: str):
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_detail(cur, symbol)
    except Exception as e:
        log.exception("heatstrip detail failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
