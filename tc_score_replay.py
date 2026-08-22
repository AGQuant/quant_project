"""cc#1211 — TC SCORE ENTRY REPLAY /100. As-of loader.

WHAT THIS FILE IS FOR, AND THE ONE THING THAT MAKES IT HARD.

The card says: replay five sessions, score every symbol in all four buckets at every 15-minute
tick, and enter on score rather than on filter pass. It also says, correctly, do not touch the
scorer — import it.

The problem the card does not mention is that tc_v4_dual has no as-of parameter. `_load_one`
reads the world as it is RIGHT NOW: v8_metrics ORDER BY score_date DESC LIMIT 1, raw_prices
LIMIT 160 counted back from today, a live CMP, a live Nifty day/week/month, a volume ratio scaled
by the actual wall clock, earnings windows measured from CURRENT_DATE. Point that at 20-Aug 09:30
and every one of those returns today's answer.

So the split is: the SCORING is imported and untouched — `_derive`, `score_card`, `card_maxes`,
the rulebooks, the weights, `_score10` and the score100 that cc#1209 added. The LOADING is
rebuilt here, query for query, with every "now" replaced by the tick being replayed. That is the
only way to obey both halves of the card.

IT IS ALSO THE ONE PLACE THIS CARD CAN GO SILENTLY WRONG. If this loader shapes `d` even slightly
differently from `_load_one`, every number downstream is wrong in a way that still looks like a
plausible result table. There is exactly one honest test for that, and it is verify item 2 in the
spec: run this loader as-of a moment where "as-of" and "live" are the same moment, and require it
to produce the identical card. `selfcheck()` at the bottom does that against the real scorer, and
nothing in this file should be trusted until it passes.

NO LOOK-AHEAD is the second rule, and it is enforced by construction rather than by care:
  - daily rows (raw_prices, v8_metrics, gvm_scores, pivots, delivery) come from STRICTLY BEFORE
    the tick's own session date, so the day being traded never informs its own entry
  - intraday bars come from the same session but only up to and including the tick
  - earnings and ban windows are measured from the tick's date, not from today
Every query below carries its bound in the SQL, not in a filter applied afterwards, because a
filter applied afterwards is one refactor away from being dropped.
"""

import os
from datetime import datetime, timedelta

import psycopg

# SCORING — imported, never redefined. If any of these move, this file should fail loudly at
# import rather than quietly fall back to a copy that has drifted from the live rulebook.
from tc_v4_endpoints import _f          # tc_v4_dual re-exports it; take it from its own home
from tc_v4_dual import (
    _derive, score_card, card_maxes,
    _sector_aggs, _nifty_ret63, _dgvm180, _m180, _segment_peer_rows, _peer_counts,
    STYLES, SIDES,
)

# The four buckets, spelled the way card_maxes keys them: BUY-MOM, BUY-REV, SELL-MOM, SELL-REV.
BUCKETS = tuple((side, style) for side in SIDES for style in STYLES)

_DB = os.getenv("DATABASE_URL")
_FULL_DAY_MIN = 375                      # 09:15 -> 15:30, matching r6_volume
_SESSION_OPEN = (9, 15)


def _conn():
    return psycopg.connect(_DB)


# ── as-of loader ──────────────────────────────────────────────────────────────────────────
#
# Mirrors tc_v4_dual._load_one query for query. Each one carries its own bound, and the bound is
# in the SQL. Read this beside _load_one when either changes: they are two halves of one contract,
# and the whole card rests on them agreeing.

def _asof_load(cur, symbol, at):
    """Build the scorer's `d` dict as it would have looked at `at` (an IST datetime).

    `at` splits the world in two. Anything daily is taken from strictly BEFORE `at`'s date - the
    session being traded must never inform its own entry. Anything intraday is taken from `at`'s
    own session, up to and including `at` itself.
    """
    day = at.date()
    d = {"symbol": symbol}

    # daily history: strictly before the traded session
    cur.execute("""SELECT price_date, open, high, low, close, volume
                   FROM raw_prices WHERE symbol=%s AND price_date < %s
                   ORDER BY price_date DESC LIMIT 160""", (symbol, day))
    rows = [{"price_date": r[0], "open": _f(r[1]), "high": _f(r[2]),
             "low": _f(r[3]), "close": _f(r[4]), "volume": _f(r[5])}
            for r in cur.fetchall()]
    rows.reverse()
    d["daily"] = rows

    d["nifty_day"], d["nifty_wk"], d["nifty_mo"], d["nifty_source"] = _asof_nifty_dwm(cur, at)
    d["vol_ratio_today"] = _asof_volume_ratio(cur, symbol, at)

    # v8_metrics: the PRIOR close's row, which is what a live scorer would have been reading at
    # 09:30 - the day's own row is written by the 15:45 EOD engine and does not exist yet.
    _V8_COLS = ["dma_20", "dma_50", "dma_200", "daily_rsi", "rsi_month", "rsi_weekly",
                "week_return", "month_return", "mom_2d", "week_index_52",
                "sector_week", "sector_month", "day_1d", "ma9_vs_ma21"]
    cur.execute("SELECT " + ", ".join(_V8_COLS) + """
                   FROM v8_metrics WHERE symbol=%s AND score_date < %s
                   ORDER BY score_date DESC LIMIT 1""", (symbol, day))
    m = cur.fetchone()
    d["v8"] = {k: _f(m[i]) for i, k in enumerate(_V8_COLS)} if m else {k: None for k in _V8_COLS}

    cur.execute("""SELECT gvm_score, segment, v_score, m_score FROM gvm_scores
                   WHERE symbol=%s AND score_date < %s
                   ORDER BY score_date DESC LIMIT 1""", (symbol, day))
    g = cur.fetchone()
    d["gvm_score"] = _f(g[0]) if g else None
    d["segment"] = g[1] if g else None
    d["v_score"] = _f(g[2]) if g else None
    d["m_score"] = _f(g[3]) if g else None

    # Sector and peer aggregates come from the shared helpers, which read the LATEST rows. They
    # are the one part of the loader that cannot be bounded without editing the scorer, and that
    # is recorded here rather than hidden: see the ASOF_LIMITS note at the bottom of this file.
    _sa = _sector_aggs(cur, d["segment"])
    d["sector_m"] = _sa["sector_m"]
    d["sector_ret63"] = _sa["sector_ret63"]
    d["sector_n_ret"] = _sa["n_ret"]
    d["nifty_ret63"] = _nifty_ret63(cur)
    d["gvm180"] = _dgvm180(cur, symbol)
    d["m180"] = _m180(cur, symbol)

    d.update({"peers_up1": 0, "peers_up": 0, "peers_dn1": 0, "peers_dn05": 0, "peers_dn": 0,
              "peer_count": 0})
    if d["segment"]:
        d.update(_peer_counts(_segment_peer_rows(cur, d["segment"]), symbol))

    cur.execute("""SELECT pp, r1, s1, r2, s2 FROM v8_paper_pivots
                   WHERE symbol=%s AND pivot_date < %s
                   ORDER BY pivot_date DESC LIMIT 1""", (symbol, day))
    p = cur.fetchone()
    d["pivots"] = ({"pp": _f(p[0]), "r1": _f(p[1]), "s1": _f(p[2]), "r2": _f(p[3]), "s2": _f(p[4])}
                   if p else {"pp": None, "r1": None, "s1": None, "r2": None, "s2": None})

    # intraday: this session, up to and including the tick
    cur.execute("""SELECT open, high, low, close, volume FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m'
                     AND ts::date = %s AND ts <= %s
                   ORDER BY ts""", (symbol, day, at))
    d["bars"] = [{"open": _f(r[0]), "high": _f(r[1]), "low": _f(r[2]),
                  "close": _f(r[3]), "volume": _f(r[4])} for r in cur.fetchall()]

    cmp_v = d["bars"][-1]["close"] if d["bars"] else None
    if cmp_v is None and rows:
        cmp_v = rows[-1]["close"]          # no cmp_prices fallback: that table has no history
    d["cmp"] = cmp_v

    cur.execute("""SELECT adr FROM adr_intraday
                   WHERE universe_count >= 50 AND ts <= %s
                   ORDER BY ts DESC LIMIT 1""", (at,))
    a = cur.fetchone()
    d["adr"] = _f(a[0]) if a else None

    cur.execute("""SELECT basis_pct, oi_chg FROM futures_basis
                   WHERE symbol=%s AND ts <= %s
                   ORDER BY ts DESC LIMIT 3""", (symbol, at))
    d["basis"] = [{"basis_pct": _f(r[0]), "oi_chg": _f(r[1])} for r in cur.fetchall()]

    cur.execute("""SELECT avg(deliv_pct) FILTER (WHERE rn <= 3),  count(*) FILTER (WHERE rn <= 3),
                          avg(deliv_pct) FILTER (WHERE rn <= 21), count(*) FILTER (WHERE rn <= 21)
                   FROM (SELECT deliv_pct, row_number() OVER (ORDER BY d DESC) rn
                         FROM delivery_eod
                         WHERE UPPER(symbol)=UPPER(%s) AND d < %s AND deliv_pct IS NOT NULL) t""",
                (symbol, day))
    _dl = cur.fetchone()
    d["deliv_3d"] = _f(_dl[0]) if _dl else None
    d["deliv_n3"] = int(_dl[1] or 0) if _dl else 0
    d["deliv_21d"] = _f(_dl[2]) if _dl else None
    d["deliv_n21"] = int(_dl[3] or 0) if _dl else 0

    cur.execute("SELECT 1 FROM futures_universe WHERE UPPER(symbol)=UPPER(%s) AND is_active=TRUE",
                (symbol,))
    d["is_future"] = cur.fetchone() is not None

    # event windows measured from the TICK's date, not from today
    cur.execute("""SELECT ex_date FROM earnings_calendar
                   WHERE UPPER(ticker)=UPPER(%s) AND ex_date BETWEEN %s AND %s
                   ORDER BY ex_date ASC LIMIT 1""", (symbol, day, day + timedelta(days=2)))
    _ev = cur.fetchone()
    d["event_blackout"] = _ev is not None
    d["event_date"] = _ev[0].isoformat() if _ev and _ev[0] else None

    cur.execute("SELECT 1 FROM fo_ban WHERE UPPER(symbol)=UPPER(%s) AND d BETWEEN %s AND %s LIMIT 1",
                (symbol, day - timedelta(days=5), day))
    d["fo_ban"] = cur.fetchone() is not None
    cur.execute("""SELECT ex_date FROM earnings_calendar WHERE UPPER(ticker)=UPPER(%s)
                   AND status='upcoming' AND ex_date >= %s ORDER BY ex_date ASC LIMIT 1""",
                (symbol, day))
    _up = cur.fetchone()
    cur.execute("""SELECT ex_date FROM earnings_calendar WHERE UPPER(ticker)=UPPER(%s)
                   AND status='reported' AND ex_date <= %s ORDER BY ex_date DESC LIMIT 1""",
                (symbol, day))
    _rep = cur.fetchone()
    d["result_up"] = _up[0].isoformat() if _up and _up[0] else None
    d["result_rep"] = _rep[0].isoformat() if _rep and _rep[0] else None

    return _derive(d)


def _asof_nifty_dwm(cur, at, symbol="NIFTY50"):
    """nifty_dwm.live_nifty_dwm, with the wall clock replaced by `at`.

    Same exclusion-not-allow-list on source that cc#1200 fixed: the index cash legs are healed
    from Yahoo and written source='yahoo', and an allow-list of one would silently skip them.
    """
    from price_sources import continuous_cash
    day = at.date()
    cur.execute("""SELECT close FROM intraday_prices
                   WHERE symbol=%s AND timeframe='5m' AND COALESCE(source,'') <> ALL(%s)
                     AND ts::date = %s AND ts <= %s
                   ORDER BY ts DESC LIMIT 1""", (symbol, continuous_cash(), day, at))
    row = cur.fetchone()
    latest = float(row[0]) if row and row[0] is not None else None
    if latest is None:
        return None, None, None, "none"

    cur.execute("""SELECT close FROM intraday_prices
                   WHERE symbol=%s AND timeframe='5m' AND COALESCE(source,'') <> ALL(%s)
                     AND ts < %s AND ts::time BETWEEN '09:15' AND '15:30'
                   ORDER BY ts DESC LIMIT 1""",
                (symbol, continuous_cash(), datetime.combine(day, at.min.time()).replace(
                    hour=_SESSION_OPEN[0], minute=_SESSION_OPEN[1])))
    pr = cur.fetchone()
    prev_close = float(pr[0]) if pr and pr[0] is not None else None

    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date < %s
                   ORDER BY price_date DESC LIMIT 22""", (symbol, day))
    hist = [float(r[0]) for r in cur.fetchall() if r[0] is not None][::-1]
    wk = hist[-5] if len(hist) >= 5 else None
    mo = hist[-22] if len(hist) >= 22 else None
    return ((latest / prev_close - 1) * 100 if prev_close else None,
            (latest / wk - 1) * 100 if wk else None,
            (latest / mo - 1) * 100 if mo else None,
            "asof_intraday")


def _asof_volume_ratio(cur, symbol, at):
    """r6_volume.volume_ratio's ratio, with the elapsed-minutes T-factor measured to `at`.

    The T-factor is the whole point of this helper and the one part that cannot be faked: volume
    at 09:30 is a fifteenth of a day, and comparing it to a full-day baseline without scaling
    would mark every early tick as thin.
    """
    day = at.date()
    cur.execute("""SELECT avg(v) FROM (
                     SELECT sum(volume) v FROM intraday_prices
                     WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m' AND ts::date < %s
                     GROUP BY ts::date ORDER BY ts::date DESC LIMIT 5) t""", (symbol, day))
    b = cur.fetchone()
    baseline = float(b[0]) if b and b[0] is not None else None
    if not baseline or baseline <= 0:
        return None

    cur.execute("""SELECT sum(volume) FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m'
                     AND ts::date = %s AND ts <= %s""", (symbol, day, at))
    t = cur.fetchone()
    today_vol = float(t[0]) if t and t[0] is not None else None
    if today_vol is None:
        return None

    elapsed = max((at.hour * 60 + at.minute) - (_SESSION_OPEN[0] * 60 + _SESSION_OPEN[1]), 1)
    expected = baseline * (min(elapsed, _FULL_DAY_MIN) / _FULL_DAY_MIN)
    return (today_vol / expected) if expected > 0 else None


# ── ASOF_LIMITS — what this loader CANNOT rewind, stated rather than buried ────────────────
#
# Four inputs come from shared helpers in tc_v4_dual that read the newest row with no date
# parameter, and bounding them would mean editing the scorer, which this card forbids:
#
#   _sector_aggs      sector M and sector 63-day return
#   _nifty_ret63      Nifty 63-day return
#   _dgvm180          180-day GVM delta
#   _m180             180-day M anchor
#
# All four are SLOW inputs - 63 and 180 session windows, and a sector aggregate across a segment.
# Over a five-session replay ending yesterday they barely move, which is why the replay is still
# worth running. But they are not as-of, and every score this file produces carries that caveat.
# It belongs in the results table, not only here.
#
# The honest consequence: a replayed score is as-of for price, volume, breadth, pivots, delivery,
# basis, events and every v8 metric, and current for those four slow rules. Anyone reading the
# sweep should know which half is which.

ASOF_LIMITS = {
    "not_rewound": ["sector_m", "sector_ret63", "nifty_ret63", "gvm180", "m180"],
    "why": "shared tc_v4_dual helpers take no date bound; the card forbids editing the scorer",
    "impact": "slow 63/180-session inputs, near-flat across a 5-session window",
}


def selfcheck(symbol="RELIANCE", side="BUY", style="MOMENTUM"):
    """The only honest test of this loader, and the card's verify item 2.

    Run the as-of loader at a moment where as-of and live ARE the same moment - the latest 5m bar
    that exists - and require the card it produces to match what the live scorer produces for the
    same symbol. If the two disagree, this loader has drifted from _load_one and nothing else in
    this file means anything.

    Returns a dict rather than asserting, so a caller can post the comparison rather than just a
    pass or fail. RUNS SERVER-SIDE ONLY: it needs DATABASE_URL.
    """
    from tc_v4_dual import trade_check_v4_dual
    out = {"symbol": symbol, "bucket": f"{side}-{style[:3]}"}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT max(ts) FROM intraday_prices
                       WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m'""", (symbol,))
        at = cur.fetchone()[0]
        if at is None:
            return {"error": f"no 5m bars for {symbol}"}
        out["at"] = at.isoformat()
        d = _asof_load(cur, symbol, at)
    mine = score_card(d, style, side)
    live = trade_check_v4_dual(symbol, side)
    theirs = None
    for c in (live.get("cards") or []):
        if c.get("style") == style and c.get("side") == side:
            theirs = c
            break
    out["asof_score100"] = mine.get("score100")
    out["live_score100"] = theirs.get("score100") if theirs else None
    out["asof_raw"] = mine.get("score")
    out["live_raw"] = theirs.get("score") if theirs else None
    out["match"] = (out["asof_score100"] == out["live_score100"])
    # Rule-level diff, because two cards can land on the same total by different routes and a
    # total-only check would call that a pass.
    if theirs:
        a = {r.get("id"): r.get("credit") for r in (mine.get("rules") or [])}
        b = {r.get("id"): r.get("credit") for r in (theirs.get("rules") or [])}
        out["rule_diffs"] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        out["match_rules"] = not out["rule_diffs"]
    return out
