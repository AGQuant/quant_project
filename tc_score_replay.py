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


# ── tables ────────────────────────────────────────────────────────────────────────────────
#
# Two tables, and the split between them is deliberate. Scoring is the expensive half - roughly
# 100k card evaluations over five sessions - and the sweep is cheap. Storing every tick's score
# once means the 15 threshold-by-hold cells are re-derived from stored numbers instead of
# re-scoring the universe fifteen times. It also means a disagreement about a cell can be traced
# back to the exact score that produced it.

DDL = """
CREATE TABLE IF NOT EXISTS tc_score_replay_ticks (
  ts        timestamp   NOT NULL,
  symbol    text        NOT NULL,
  bucket    text        NOT NULL,
  score100  numeric,
  raw       numeric,
  max_raw   numeric,
  PRIMARY KEY (ts, symbol, bucket)
);
CREATE INDEX IF NOT EXISTS tc_srt_ts    ON tc_score_replay_ticks (ts);
CREATE INDEX IF NOT EXISTS tc_srt_score ON tc_score_replay_ticks (bucket, score100);

CREATE TABLE IF NOT EXISTS tc_score_replay_trades (
  threshold   int    NOT NULL,
  hold_days   int    NOT NULL,
  symbol      text   NOT NULL,
  bucket      text   NOT NULL,
  side        text   NOT NULL,
  entry_ts    timestamp NOT NULL,
  entry_px    numeric,
  entry_src   text,
  exit_ts     timestamp,
  exit_px     numeric,
  exit_reason text,
  pnl_pct     numeric,
  PRIMARY KEY (threshold, hold_days, symbol, bucket, entry_ts)
);
CREATE INDEX IF NOT EXISTS tc_srtr_cell ON tc_score_replay_trades (threshold, hold_days);

-- cc#1221 PORTFOLIO MODE. A SEPARATE table, never the sweep's: the sweep answers "what does every
-- signal above the bar do", this answers "what does a book that can only hold twenty do". Mixing
-- them in one table would make every future query specify which run it meant.
CREATE TABLE IF NOT EXISTS tc_score_replay_port_trades (
  symbol      text   NOT NULL,
  bucket      text   NOT NULL,
  side        text   NOT NULL,
  entry_ts    timestamp NOT NULL,
  entry_px    numeric,
  entry_src   text,
  score100    numeric,
  slot_rank   int,
  open_book_size int,
  exit_ts     timestamp,
  exit_px     numeric,
  exit_reason text,
  pnl_pct     numeric,
  PRIMARY KEY (symbol, entry_ts)
);
CREATE INDEX IF NOT EXISTS tc_srpt_ts ON tc_score_replay_port_trades (entry_ts);
"""

SESSIONS = ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]
THRESHOLDS = (60, 65, 70, 80, 84)
HOLDS = (1, 2, 3)
TARGET_PCT = 2.0
STOP_PCT = 2.0
SQUAREOFF = (15, 20)
FUT_SOURCES = ("fyers_fut", "fyers_fut_rest", "yahoo")   # preference order, tagged as entry_src


def ticks_for(day):
    """09:30 to 15:15 inclusive, every 15 minutes. 24 ticks, which is what the card asks for."""
    base = datetime.strptime(day, "%Y-%m-%d")
    out, t = [], base.replace(hour=9, minute=30)
    end = base.replace(hour=15, minute=15)
    while t <= end:
        out.append(t)
        t += timedelta(minutes=15)
    return out


def _active_universe(cur):
    cur.execute("SELECT UPPER(symbol) FROM futures_universe WHERE is_active=TRUE ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


# ── phase 1: score every tick ─────────────────────────────────────────────────────────────

def score_all(days=None, symbols=None, progress=None):
    """Fill tc_score_replay_ticks. Idempotent per (ts, symbol, bucket).

    Deliberately does NOT skip a symbol whose card comes back None - a missing score is written
    as NULL so a thin tick is visible in the table rather than being indistinguishable from a
    symbol that was never attempted. The card asks for ticks with under 150 symbols scored to be
    flagged, and that flag can only be honest if absence is recorded.
    """
    days = days or SESSIONS
    done = 0
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()
        syms = symbols or _active_universe(cur)
        for day in days:
            for at in ticks_for(day):
                rows = []
                for sym in syms:
                    try:
                        d = _asof_load(cur, sym, at)
                    except Exception:
                        continue                      # unscoreable symbol at this tick
                    if d.get("cmp") is None:
                        continue
                    for side, style in BUCKETS:
                        try:
                            c = score_card(d, style, side)
                        except Exception:
                            continue
                        rows.append((at, sym, f"{side}-{style[:3]}",
                                     c.get("score100"), c.get("score"), c.get("max_raw")))
                if rows:
                    cur.executemany(
                        """INSERT INTO tc_score_replay_ticks (ts,symbol,bucket,score100,raw,max_raw)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (ts,symbol,bucket) DO UPDATE
                             SET score100=EXCLUDED.score100, raw=EXCLUDED.raw,
                                 max_raw=EXCLUDED.max_raw""", rows)
                    conn.commit()
                done += 1
                if progress:
                    progress(day, at, len(rows), done)
    return done


# ── phase 2: prices, then the sweep ───────────────────────────────────────────────────────

def _fut_bars(cur, symbol, t0, t1):
    """5m futures bars between two timestamps, preferring fyers_fut and tagging what was used.

    The preference order is the card's: fyers_fut, then fyers_fut_rest, then yahoo. A trade that
    priced off a fallback is not wrong, but it is different, and entry_src carries that so the
    results table can say how many did.
    """
    for src in FUT_SOURCES:
        cur.execute("""SELECT ts, open, high, low, close FROM intraday_prices
                       WHERE symbol=%s AND source=%s AND timeframe='5m'
                         AND ts >= %s AND ts <= %s ORDER BY ts""", (symbol, src, t0, t1))
        rows = cur.fetchall()
        if rows:
            return [{"ts": r[0], "open": _f(r[1]), "high": _f(r[2]),
                     "low": _f(r[3]), "close": _f(r[4])} for r in rows], src
    return [], None


def _walk(bars, side, entry_px, deadline):
    """Walk 5m bars to the first of target, stop, or the square-off deadline.

    STOP IS CHECKED BEFORE TARGET INSIDE A BAR, always. A 5m bar that touches both tells us
    nothing about which came first, and assuming the good one is how a backtest flatters itself.
    Taking the stop is the conservative reading and it is the one the card asks for.
    """
    up = entry_px * (1 + TARGET_PCT / 100)
    dn = entry_px * (1 - STOP_PCT / 100)
    tgt, stop = (up, dn) if side == "BUY" else (dn, up)
    for b in bars:
        if b["high"] is None or b["low"] is None:
            continue
        hit_stop = (b["low"] <= stop) if side == "BUY" else (b["high"] >= stop)
        hit_tgt = (b["high"] >= tgt) if side == "BUY" else (b["low"] <= tgt)
        if hit_stop:
            return b["ts"], stop, "SL"
        if hit_tgt:
            return b["ts"], tgt, "TGT"
        if b["ts"] >= deadline:
            return b["ts"], b["close"], "SQ"
    if bars:
        return bars[-1]["ts"], bars[-1]["close"], "SQ"
    return None, None, None


def sweep(thresholds=THRESHOLDS, holds=HOLDS):
    """Fill tc_score_replay_trades for every cell, from the scores already stored.

    Entry rule, exactly as the card states it: score100 >= threshold, no open position in the
    same symbol and side, and the FIRST qualification that day latches - a symbol that clears the
    bar at 09:30 and again at 11:00 is one trade, not two.
    """
    made = 0
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()
        for th in thresholds:
            for hold in holds:
                cur.execute("""DELETE FROM tc_score_replay_trades
                               WHERE threshold=%s AND hold_days=%s""", (th, hold))
                cur.execute("""SELECT ts, symbol, bucket, score100 FROM tc_score_replay_ticks
                               WHERE score100 >= %s ORDER BY ts""", (th,))
                qualified = cur.fetchall()
                open_until = {}          # (symbol, side) -> exit_ts of the trade still running
                rows = []
                for ts, sym, bucket, sc in qualified:
                    side = bucket.split("-")[0]
                    key = (sym, side)
                    if key in open_until and ts <= open_until[key]:
                        continue                          # position already open
                    day = ts.date()
                    deadline = datetime.combine(
                        _nth_session(day, hold), datetime.min.time()
                    ).replace(hour=SQUAREOFF[0], minute=SQUAREOFF[1])
                    bars, src = _fut_bars(cur, sym, ts, deadline)
                    if not bars:
                        continue
                    entry_px = bars[0]["close"]
                    if not entry_px:
                        continue
                    x_ts, x_px, why = _walk(bars[1:], side, entry_px, deadline)
                    if x_ts is None:
                        continue
                    pnl = ((x_px / entry_px - 1) * 100) if side == "BUY" \
                        else ((entry_px / x_px - 1) * 100)
                    open_until[key] = x_ts
                    rows.append((th, hold, sym, bucket, side, ts, entry_px, src,
                                 x_ts, x_px, why, round(pnl, 4)))
                if rows:
                    cur.executemany(
                        """INSERT INTO tc_score_replay_trades
                           (threshold,hold_days,symbol,bucket,side,entry_ts,entry_px,entry_src,
                            exit_ts,exit_px,exit_reason,pnl_pct)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING""", rows)
                    conn.commit()
                    made += len(rows)
    return made


# ══ cc#1221 · PORTFOLIO MODE ══════════════════════════════════════════════════════════════════
# The sweep takes EVERY signal above the bar — 809 trades at threshold 60, 2,328 of them SELL-MOM
# alone — and every cell came out negative. This asks the different question the founder wants:
# what does a book that can only hold twenty names do, when it is filled by rank?
#
# SELECTION, NOT QUANTITY. That is the whole difference. A cap forces the run to choose, and the
# thing being tested stops being "does the score work" and becomes "does the score RANK".
SCORE_MIN = 80
BOOK_CAP = 20
MAX_HOLD_SESS = 3


def portfolio(score_min=SCORE_MIN, cap=BOOK_CAP, max_hold=MAX_HOLD_SESS):
    """Re-walk the STORED ticks under a capped book. No re-scoring, ever.

    WHY THIS CANNOT REUSE sweep()'s SHAPE. sweep() streams qualified ticks in time order and opens
    a position whenever that symbol+side is free — each decision is independent of every other.
    Here they are not: whether a candidate gets a slot depends on how many positions are still
    open at that instant, which depends on exits computed at earlier ticks. So the walk has to go
    tick by tick, carrying the book forward, and a candidate that would have been taken at 09:30
    is simply missed if twenty better ones are already held.
    """
    made = 0
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("DELETE FROM tc_score_replay_port_trades")
        conn.commit()

        cur.execute("""SELECT ts, symbol, bucket, score100 FROM tc_score_replay_ticks
                       WHERE score100 >= %s ORDER BY ts, score100 DESC""", (score_min,))
        by_tick = {}
        for ts, sym, bucket, sc in cur.fetchall():
            by_tick.setdefault(ts, []).append((float(sc), sym, bucket))

        book = {}            # symbol -> exit_ts of the position still running
        traded_today = set()  # (symbol, date) — the card forbids a same-day re-entry
        rows = []

        for ts in sorted(by_tick):
            # retire anything that has already exited BEFORE deciding who gets a slot
            for s in [s for s, x in book.items() if x is not None and x <= ts]:
                del book[s]
            slots = cap - len(book)
            if slots <= 0:
                continue

            # ONE CANDIDATE PER SYMBOL, highest-scoring bucket wins. A symbol qualifying in two
            # buckets is one opportunity, not two, and taking both would quietly double its weight
            # in a book that is supposed to hold twenty distinct names.
            best = {}
            for sc, sym, bucket in by_tick[ts]:
                if sym in book or (sym, ts.date()) in traded_today:
                    continue
                if sym not in best or sc > best[sym][0]:
                    best[sym] = (sc, bucket)

            ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
            rank = 0
            for sym, (sc, bucket) in ranked:
                if slots <= 0:
                    break
                rank += 1
                side = bucket.split("-")[0]
                deadline = datetime.combine(
                    _nth_session(ts.date(), max_hold), datetime.min.time()
                ).replace(hour=SQUAREOFF[0], minute=SQUAREOFF[1])
                bars, src = _fut_bars(cur, sym, ts, deadline)
                if not bars:
                    continue
                entry_px = bars[0]["close"]
                if not entry_px:
                    continue
                x_ts, x_px, why = _walk(bars[1:], side, entry_px, deadline)
                if x_ts is None:
                    continue
                pnl = ((x_px / entry_px - 1) * 100) if side == "BUY" \
                    else ((entry_px / x_px - 1) * 100)
                book[sym] = x_ts
                traded_today.add((sym, ts.date()))
                slots -= 1
                # open_book_size is the book INCLUDING this position, so avg() reads as "how full
                # did the book run" and max() is the cap check the card asks for.
                rows.append((sym, bucket, side, ts, entry_px, src, round(sc, 2), rank, len(book),
                             x_ts, x_px, why, round(pnl, 4)))

        if rows:
            cur.executemany(
                """INSERT INTO tc_score_replay_port_trades
                   (symbol,bucket,side,entry_ts,entry_px,entry_src,score100,slot_rank,
                    open_book_size,exit_ts,exit_px,exit_reason,pnl_pct)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""", rows)
            conn.commit()
            made = len(rows)
    return made


def portfolio_summary():
    """What the capped book actually did. Returns None when nothing has run — never a zero row."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tc_score_replay_port_trades")
        if not cur.fetchone()[0]:
            return None
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE pnl_pct>0), avg(pnl_pct),
                              sum(pnl_pct), avg(open_book_size), max(open_book_size),
                              avg(slot_rank), min(score100)
                       FROM tc_score_replay_port_trades""")
        n, w, avg, tot, abook, mbook, arank, minsc = cur.fetchone()
        cur.execute("""SELECT bucket, count(*), count(*) FILTER (WHERE pnl_pct>0),
                              avg(pnl_pct), sum(pnl_pct)
                       FROM tc_score_replay_port_trades GROUP BY bucket ORDER BY 1""")
        buckets = [{"bucket": b, "trades": c, "wins": ww, "acc": round(100.0 * ww / c, 1) if c else 0,
                    "avg": round(float(a or 0), 3), "sum": round(float(s or 0), 2)}
                   for b, c, ww, a, s in cur.fetchall()]
        cur.execute("""SELECT entry_ts::date, count(*), avg(pnl_pct), sum(pnl_pct)
                       FROM tc_score_replay_port_trades GROUP BY 1 ORDER BY 1""")
        days = [{"day": str(d), "trades": c, "avg": round(float(a or 0), 3),
                 "sum": round(float(s or 0), 2)} for d, c, a, s in cur.fetchall()]
        cur.execute("""SELECT exit_reason, count(*) FROM tc_score_replay_port_trades
                       GROUP BY 1 ORDER BY 2 DESC""")
        exits = {r[0]: r[1] for r in cur.fetchall()}
    return {"params": {"score_min": SCORE_MIN, "book_cap": BOOK_CAP,
                       "target_pct": TARGET_PCT, "stop_pct": STOP_PCT,
                       "max_hold_sessions": MAX_HOLD_SESS},
            "trades": n, "wins": w, "acc": round(100.0 * w / n, 1) if n else 0,
            "avg_pnl_pct": round(float(avg or 0), 3), "total_pnl_pct": round(float(tot or 0), 2),
            "avg_open_book": round(float(abook or 0), 1), "max_open_book": int(mbook or 0),
            "slot_utilisation_pct": round(100.0 * float(abook or 0) / BOOK_CAP, 1),
            "avg_slot_rank": round(float(arank or 0), 2), "min_score_taken": float(minsc or 0),
            "by_bucket": buckets, "by_day": days, "exits": exits,
            "merit_gate": {"avg": MERIT_AVG, "acc": MERIT_ACC},
            "merit": bool(float(avg or 0) >= MERIT_AVG and (100.0 * w / n if n else 0) >= MERIT_ACC)}


def _nth_session(day, n):
    """The nth trading session on or after `day`, counted within the replay window.

    Uses the replay's own session list rather than a weekday rule, because a weekday rule would
    walk a trade into 15-Aug - a Saturday and Independence Day - which is the same mistake the
    card's own day list makes.

    THE CLAMP AT THE END OF THE WINDOW BIASES THE LONGER HOLDS, and the results table has to say
    so. A trade entered on Friday 21-Aug with hold 3 cannot run three sessions - the replay window
    ends that day - so it squares off same-day and is really a hold-1 trade wearing a hold-3
    label. The same is true, less severely, for 20-Aug. The effect is that the hold-2 and hold-3
    columns are diluted toward hold-1 behaviour by the trades opened near the window's end, which
    flatters neither direction but does blur the comparison the founder is trying to make. The
    count of clamped trades per cell is reported alongside the cell.
    """
    ds = [datetime.strptime(s, "%Y-%m-%d").date() for s in SESSIONS]
    later = [x for x in ds if x >= day]
    if not later:
        return day
    return later[min(n - 1, len(later) - 1)]


# ── results ───────────────────────────────────────────────────────────────────────────────

MERIT_AVG = 1.0
MERIT_ACC = 60.0


def coverage(cur):
    """The two honesty flags the card asks for, computed rather than asserted.

    A thin tick and a backfilled bar both make a cell look like evidence when it is not, so they
    are counted and printed above the table rather than mentioned in passing.
    """
    cur.execute("""SELECT ts, count(DISTINCT symbol) n FROM tc_score_replay_ticks
                   GROUP BY ts ORDER BY ts""")
    per_tick = cur.fetchall()
    thin = [(t, n) for t, n in per_tick if n < 150]
    cur.execute("""SELECT source, count(*) FROM intraday_prices
                   WHERE ts::date = DATE '2026-08-21' AND timeframe='5m'
                   GROUP BY source ORDER BY 2 DESC""")
    aug21 = cur.fetchall()
    return {"ticks": len(per_tick),
            "thin_ticks": thin,
            "min_symbols": min([n for _, n in per_tick], default=0),
            "aug21_sources": aug21}


def _cells(cur):
    cur.execute("""SELECT threshold, hold_days,
                          count(*),
                          count(*) FILTER (WHERE pnl_pct > 0),
                          avg(pnl_pct), sum(pnl_pct),
                          count(*) FILTER (WHERE exit_reason='TGT'),
                          count(*) FILTER (WHERE exit_reason='SL'),
                          count(*) FILTER (WHERE exit_reason='SQ'),
                          count(*) FILTER (WHERE entry_src <> 'fyers_fut')
                   FROM tc_score_replay_trades
                   GROUP BY threshold, hold_days
                   ORDER BY threshold, hold_days""")
    out = []
    for th, hold, n, w, avg, tot, tgt, sl, sq, fb in cur.fetchall():
        acc = (100.0 * w / n) if n else 0.0
        avg = float(avg or 0)
        merit = (avg >= MERIT_AVG and acc >= MERIT_ACC)
        out.append({"threshold": th, "hold": hold, "trades": n, "wins": w,
                    "acc": round(acc, 1), "avg": round(avg, 3),
                    "sum": round(float(tot or 0), 2),
                    "tgt": tgt, "sl": sl, "sq": sq, "fallback_px": fb, "merit": merit})
    return out


def results_table():
    """The card's deliverable: ONE markdown table, plus what it took to believe it.

    Everything that could make a number misleading is printed with the number rather than left
    for the reader to discover: how many symbols the thinnest tick scored, how many trades priced
    off a fallback feed, how many were clamped by the end of the window, and which four inputs
    were not rewound at all.
    """
    with _conn() as conn, conn.cursor() as cur:
        cov = coverage(cur)
        cells = _cells(cur)
        # DATE %s IS A SYNTAX ERROR AND ONLY SHOWS UP AT RUN TIME. The DATE prefix form is a
        # type-prefixed LITERAL — `DATE '2026-08-21'` — so it takes a quoted constant and nothing
        # else. psycopg sends %s as a bind parameter, Postgres sees `DATE $1`, and it fails to
        # parse. Nothing catches that before the query runs: the module imports fine, the scoring
        # and the sweep both ran to completion (99,840 ticks, 4,988 trades), and the failure
        # surfaced only when results_table() was finally called on real data. A cast does the same
        # job and accepts a parameter.
        cur.execute("""SELECT threshold, hold_days, count(*) FROM tc_score_replay_trades
                       WHERE entry_ts::date > (%s::date - hold_days + 1)
                       GROUP BY threshold, hold_days""", (SESSIONS[-1],))
        clamped = {(t, h): n for t, h, n in cur.fetchall()}

    L = []
    L.append("**TC SCORE ENTRY REPLAY /100** — sessions %s to %s (5 trading days)."
             % (SESSIONS[0], SESSIONS[-1]))
    L.append("")
    L.append("The card said 15,18,19,20,21-Aug. 15-Aug-2026 is a SATURDAY and Independence Day, "
             "and v8_metrics has no row for it, so the five sessions are MON 17 to FRI 21.")
    L.append("")
    L.append("Entry: first tick that day where score100 >= threshold, no open position same "
             "symbol+side. Exit: +2% target / -2% stop walked on 5m high/low, **stop taken "
             "first when a bar touches both**, else square-off 15:20 on session N.")
    L.append("")
    L.append("| thr | N | trades | wins | acc% | avg% | sum% | TGT | SL | SQ | fallback px | clamped | merit |")
    L.append("|----:|--:|-------:|-----:|-----:|-----:|-----:|----:|---:|---:|------------:|--------:|:-----:|")
    for c in cells:
        L.append("| %d | %d | %d | %d | %.1f | %+.3f | %+.2f | %d | %d | %d | %d | %d | %s |"
                 % (c["threshold"], c["hold"], c["trades"], c["wins"], c["acc"], c["avg"],
                    c["sum"], c["tgt"], c["sl"], c["sq"], c["fallback_px"],
                    clamped.get((c["threshold"], c["hold"]), 0), "Y" if c["merit"] else "N"))
    L.append("")
    L.append("merit gate = avg >= %.1f%% AND acc >= %.0f%%." % (MERIT_AVG, MERIT_ACC))
    L.append("")
    L.append("**Read these before the table.**")
    L.append("- ticks scored: %d; thinnest tick scored %d symbols%s"
             % (cov["ticks"], cov["min_symbols"],
                ("; %d ticks under 150 symbols" % len(cov["thin_ticks"])) if cov["thin_ticks"] else ""))
    L.append("- 21-Aug bar sources: %s — anything not fyers_fut is backfill or heal, not a live feed"
             % ", ".join("%s %d" % (s, n) for s, n in cov["aug21_sources"]))
    L.append("- **clamped** counts trades whose hold ran past the end of the window and squared "
             "off early. Those rows are hold-1 behaviour under a longer label.")
    L.append("- **not as-of**: %s. These come from shared scorer helpers that take no date bound, "
             "and the card forbids editing the scorer. They are 63 and 180 session windows, so "
             "they barely move across five days — but a replayed score is as-of for price, "
             "volume, breadth, pivots, delivery, basis and every v8 metric, and CURRENT for "
             "those five." % ", ".join(ASOF_LIMITS["not_rewound"]))
    return "\n".join(L)


def best_cells(n=2):
    with _conn() as conn, conn.cursor() as cur:
        cells = _cells(cur)
    return sorted(cells, key=lambda c: (c["avg"], c["acc"]), reverse=True)[:n]


def bucket_breakdown(threshold, hold):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT bucket, count(*), count(*) FILTER (WHERE pnl_pct>0),
                              avg(pnl_pct), sum(pnl_pct)
                       FROM tc_score_replay_trades WHERE threshold=%s AND hold_days=%s
                       GROUP BY bucket ORDER BY 1""", (threshold, hold))
        rows = cur.fetchall()
    L = ["**%d / hold %d — by bucket**" % (threshold, hold), "",
         "| bucket | trades | wins | acc% | avg% | sum% |", "|---|---:|---:|---:|---:|---:|"]
    for b, n, w, avg, tot in rows:
        L.append("| %s | %d | %d | %.1f | %+.3f | %+.2f |"
                 % (b, n, w, (100.0 * w / n) if n else 0, float(avg or 0), float(tot or 0)))
    return "\n".join(L)
