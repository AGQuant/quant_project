"""
qsr_engine.py — QSR (Quality S1 Reclaim) V1, the daily EOD equity swing engine.

SPEC: session_log 27980, founder-locked 20-Aug-2026. Card cc#1175.
THESIS: rising quality, calm price, at support, reclaiming. Daily EOD scan over the full GVM
universe (~1,794 today), distinct from all five existing theses.

WHY THE FUNNEL LEDGER IS WRITTEN FIRST AND ALWAYS
    27980 calls it non-negotiable and names the reason out loud: V14 shipped without one, and when
    it produced nothing nobody could tell a day where the market offered no setup from a day where
    the engine never ran. qsr_funnel_daily is written on EVERY scan, including — especially — a
    scan that qualifies nobody, and it carries per-stage FAIL LISTS as well as counts, so a zero
    day says which gate emptied it rather than merely that it was empty.

CONTEXT ISOLATION (rule 7)
    Every table this module writes is qsr_*. It READS gvm_scores, gvm_history, raw_prices,
    v8_paper_pivots and delivery_eod, and writes none of them. No v8_/tc_/v14_ table is touched.

WHAT IT READS, AND WHY EACH SOURCE (all verified by query before this file was written, cc#1175
data-gate answer in the forum)
    gvm_scores      universe, current GVM, segment            1,794 symbols @ 2026-08-20
    gvm_history     the 90-day-ago GVM for dGVM_90d           1.55M rows, 2021-07-07 onward
    raw_prices      closes, lows, volumes                     1,836 symbols
    v8_paper_pivots rolling-5d PP / S1 — UNIVERSE-WIDE        1,835 symbols @ 2026-08-21
                    (built by gvm_universe_pivots for every symbol in raw_prices, so QSR does no
                     pivot maths of its own and cannot disagree with the rest of the platform)
    delivery_eod    deliv_qty for the VolD leg                from 2026-07-20, 2,690 symbols

    ON delivery_eod AND THE LOCK. 27980 says delivery data "exists only since 07-Aug" and rules
    that a null VolD leg auto-fails while 2-of-3 stays reachable via legs a+c. Measured, it
    actually starts 2026-07-20 — about 21 sessions per symbol — so the 20-day average IS
    computable for most of the universe today and the null path fires rarely. The null path is
    built exactly as ruled anyway, and the payload always states which legs passed and why, but
    the funnel should run richer than the lock expected.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import psycopg

log = logging.getLogger("scorr.qsr")

# ── founder-locked constants (27980). Every one of these is a number the founder set; none is a
# ── default I chose. They are named so a reader can find the lock line that set each.
GVM_MIN          = 7.0     # quality: GVM > 7
DGVM_90D_MIN     = 0.5     # quality: dGVM_90d >= +0.5
SECTOR_MULT      = 1.5     # sector: segment week avg > NIFTY week * 1.5
DAY_BAND         = (0.0, 3.0)
WEEK_BAND        = (0.0, 5.0)
MONTH_BAND       = (0.0, 10.0)
S1_TOUCH_TOL     = 1.005   # low <= S1 * 1.005, within the last 3 sessions
S1_TOUCH_SESSIONS = 3
VOL_RATIO        = 1.1     # all three volume legs use 1.1x
VOL_LEGS_NEEDED  = 2       # 2-of-3, founder-ruled after strict ANDs returned zero
ACC_SESSIONS     = 30      # accumulation window
MAX_OPEN         = 15
MAX_NEW_PER_DAY  = 3
NOTIONAL         = 100000.0   # 1L per position, paper
HARD_STOP_PCT    = -8.0
TIME_STOP_SESS   = 30
QUALITY_BREAK_GVM = 7.0    # exit when nightly GVM drops below this

# A segment with too few members cannot produce a meaningful average, so its sector leg goes
# NEUTRAL (passes) and the funnel says so, rather than failing a stock for a thin peer group.
SECTOR_MIN_MEMBERS = 3

# Fail lists are capped so one bad day cannot write a megabyte of JSON. The COUNTS are always
# exact; only the named examples are truncated, and the payload says when it truncated.
FAIL_LIST_CAP = 40


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_now():
    """Naive IST, the convention every engine in this repo uses (cc#844: never shift a tz-aware
    value in Python — these are stored and compared as naive IST throughout)."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


# ── the scan ─────────────────────────────────────────────────────────────────────────────────
# One SQL pass builds every per-symbol input the gates need. It is one query rather than five
# because the stages share the same price window, and reading raw_prices five times over 1,800
# symbols to answer questions about the same 30 bars is work for nothing.
_SCAN_SQL = """
WITH latest AS (SELECT MAX(score_date) AS d FROM gvm_scores),
piv_d AS (SELECT MAX(pivot_date) AS d FROM v8_paper_pivots),
cur AS (
    SELECT g.symbol, g.gvm_score, g.segment
    FROM gvm_scores g, latest
    WHERE g.score_date = latest.d AND g.gvm_score IS NOT NULL
),
-- the most recent GVM at or before the 90-day mark. DISTINCT ON, not a window with FILTER:
-- Postgres allows FILTER on aggregates only, and the windowed form is a syntax error, not a
-- slow query (the cc#1174 lesson, paid for once already).
past AS (
    SELECT DISTINCT ON (h.symbol) h.symbol, h.gvm_score AS gvm_90
    FROM gvm_history h, latest
    WHERE h.score_date <= latest.d - 90 AND h.gvm_score IS NOT NULL
    ORDER BY h.symbol, h.score_date DESC
),
px AS (
    SELECT symbol, price_date, close, low, volume,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
    FROM raw_prices WHERE close IS NOT NULL
),
bars AS (
    SELECT symbol,
           MAX(CASE WHEN rn = 1  THEN close END)  AS c0,
           MAX(CASE WHEN rn = 2  THEN close END)  AS c1,
           MAX(CASE WHEN rn = 6  THEN close END)  AS c5,
           MAX(CASE WHEN rn = 22 THEN close END)  AS c21,
           MIN(CASE WHEN rn <= %(s1_sess)s THEN low END) AS low3,
           MAX(CASE WHEN rn = 1  THEN volume END)::numeric AS v0,
           AVG(volume) FILTER (WHERE rn BETWEEN 2 AND 22)  AS v21avg
    FROM px WHERE rn <= 22 GROUP BY symbol
),
acc AS (
    SELECT symbol,
           SUM(CASE WHEN close > prev THEN volume ELSE 0 END)::numeric AS upvol,
           SUM(CASE WHEN close < prev THEN volume ELSE 0 END)::numeric AS dnvol
    FROM (
        SELECT symbol, close, volume,
               LAG(close) OVER (PARTITION BY symbol ORDER BY price_date) AS prev,
               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
        FROM raw_prices WHERE close IS NOT NULL
    ) z WHERE rn <= %(acc_sess)s GROUP BY symbol
),
dl AS (
    SELECT symbol,
           MAX(CASE WHEN rn = 1 THEN deliv_qty END)::numeric AS d0,
           AVG(deliv_qty) FILTER (WHERE rn BETWEEN 2 AND 21)  AS d20avg
    FROM (
        SELECT symbol, deliv_qty,
               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn
        FROM delivery_eod WHERE deliv_qty IS NOT NULL
    ) y WHERE rn <= 21 GROUP BY symbol
)
SELECT c.symbol, c.gvm_score, c.segment, p.gvm_90,
       b.c0, b.c1, b.c5, b.c21, b.low3, b.v0, b.v21avg,
       a.upvol, a.dnvol, dl.d0, dl.d20avg,
       v.pp, v.s1
FROM cur c
LEFT JOIN past  p  ON p.symbol  = c.symbol
LEFT JOIN bars  b  ON b.symbol  = c.symbol
LEFT JOIN acc   a  ON a.symbol  = c.symbol
LEFT JOIN dl       ON dl.symbol = c.symbol
LEFT JOIN v8_paper_pivots v ON v.symbol = c.symbol AND v.pivot_date = (SELECT d FROM piv_d)
"""


def _pct(a, b):
    """Percent change b -> a. None when either side is missing or the base is zero — never 0.0,
    which would read as 'flat' and quietly pass a band that should have had no answer.

    ROUNDED TO 6 PLACES, AND THAT IS A CORRECTNESS FIX, NOT TIDINESS. The founder's bands are
    inclusive: day 0-3, week 0-5, month 0-10. In binary floating point (103/100 - 1) * 100 is
    3.0000000000000027, so a stock closing exactly +3.00% would have FAILED the day band by
    2.7e-15 — a real name rejected for a representation artefact, on a boundary the lock wrote as
    included. Six places is far finer than any threshold here and removes the noise entirely.
    Caught by asserting the helper against a hand-computed 3.0 rather than by reading it.
    """
    if a is None or b is None:
        return None
    a, b = float(a), float(b)
    if b == 0:
        return None
    return round((a / b - 1.0) * 100.0, 6)


def _in_band(v, band):
    return v is not None and band[0] <= v <= band[1]


def _sector_week(rows):
    """Segment week-return averages, and the NIFTY week to compare them against.

    MEMBER FLOOR: a segment with fewer than SECTOR_MIN_MEMBERS priced members returns None and the
    caller treats the leg as NEUTRAL. An average over one or two names is not a sector reading,
    and failing a stock because its peer group is thin would be the gate punishing a data shape
    rather than the market.
    """
    agg = {}
    for r in rows:
        seg, wk = r["segment"], _pct(r["c0"], r["c5"])
        if not seg or wk is None:
            continue
        agg.setdefault(seg, []).append(wk)
    return {s: (sum(v) / len(v)) for s, v in agg.items() if len(v) >= SECTOR_MIN_MEMBERS}, \
           {s: len(v) for s, v in agg.items()}


def _nifty_week(cur):
    cur.execute("""SELECT close FROM raw_prices WHERE symbol = 'NIFTY50' AND close IS NOT NULL
                   ORDER BY price_date DESC LIMIT 6""")
    cl = [float(x[0]) for x in cur.fetchall()]
    return _pct(cl[0], cl[5]) if len(cl) >= 6 else None


def _volume_legs(r):
    """The 2-of-3 soft gate, founder-ruled after the strict ANDs returned zero on 20-Aug.

    Returns (passed_count, detail). Every leg reports pass/fail AND why, because the payload has
    to state which legs carried a name — a bare '2 of 3' cannot be checked by anyone.

    A NULL VolD LEG AUTO-FAILS AND SAYS SO, exactly as ruled. It never counts as a pass and it
    never blocks the other two: 2-of-3 stays reachable through legs a and c alone.
    """
    detail, passed = {}, 0

    v0, v21 = r["v0"], r["v21avg"]
    ok_a = v0 is not None and v21 and float(v0) >= VOL_RATIO * float(v21)
    detail["volx"] = {"pass": bool(ok_a),
                      "ratio": round(float(v0) / float(v21), 3) if (v0 is not None and v21) else None,
                      "why": "today volume vs 21d avg, need >= %.1fx" % VOL_RATIO}
    passed += bool(ok_a)

    d0, d20 = r["d0"], r["d20avg"]
    if d0 is None or not d20:
        ok_b = False
        detail["vold"] = {"pass": False, "ratio": None,
                          "why": "delivery volume unavailable for this symbol — leg AUTO-FAILS "
                                 "per 27980; 2-of-3 still reachable via volx + accumulation"}
    else:
        ok_b = float(d0) >= VOL_RATIO * float(d20)
        detail["vold"] = {"pass": bool(ok_b), "ratio": round(float(d0) / float(d20), 3),
                          "why": "delivery qty vs its 20d avg, need >= %.1fx" % VOL_RATIO}
    passed += bool(ok_b)

    up, dn = r["upvol"], r["dnvol"]
    ok_c = up is not None and dn and float(up) >= VOL_RATIO * float(dn)
    detail["accumulation"] = {"pass": bool(ok_c),
                              "ratio": round(float(up) / float(dn), 3) if (up is not None and dn) else None,
                              "why": "%dd up-close vol / down-close vol, need >= %.1fx"
                                     % (ACC_SESSIONS, VOL_RATIO)}
    passed += bool(ok_c)

    return passed, detail


def scan(conn=None):
    """One EOD scan. Returns the funnel dict; writes nothing. run_qsr_scan() persists."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCAN_SQL, {"s1_sess": S1_TOUCH_SESSIONS, "acc_sess": ACC_SESSIONS})
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            nifty_wk = _nifty_week(cur)

        sector_avg, sector_n = _sector_week(rows)

        # Stages are evaluated in gate order and every drop is RECORDED against the stage that
        # dropped it. A survivor list alone cannot answer "why was today empty".
        fails = {"quality": [], "sector": [], "returns": [], "location": [], "volume": []}
        stage = {"quality": 0, "sector": 0, "returns": 0, "location": 0, "volume": 0}
        qualified = []

        for r in rows:
            sym = r["symbol"]
            gvm = float(r["gvm_score"]) if r["gvm_score"] is not None else None
            dgvm = (gvm - float(r["gvm_90"])) if (gvm is not None and r["gvm_90"] is not None) else None

            if not (gvm is not None and gvm > GVM_MIN and dgvm is not None and dgvm >= DGVM_90D_MIN):
                fails["quality"].append(sym)
                continue
            stage["quality"] += 1

            seg_wk = sector_avg.get(r["segment"])
            if seg_wk is None:
                sector_ok, sector_note = True, "neutral — segment has %d priced members (< %d)" % (
                    sector_n.get(r["segment"], 0), SECTOR_MIN_MEMBERS)
            elif nifty_wk is None:
                sector_ok, sector_note = True, "neutral — NIFTY week unavailable"
            else:
                # The second clause is the founder's guard: on a week when NIFTY is NEGATIVE,
                # "> nifty * 1.5" is satisfied by any segment less bad than the index, which is
                # not strength. Requiring the segment to be positive as well is what makes this
                # a leadership test rather than an arithmetic accident.
                sector_ok = seg_wk > nifty_wk * SECTOR_MULT and seg_wk > 0
                sector_note = "segment week %.2f%% vs NIFTY %.2f%% x %.1f" % (seg_wk, nifty_wk, SECTOR_MULT)
            if not sector_ok:
                fails["sector"].append(sym)
                continue
            stage["sector"] += 1

            d_ret, w_ret, m_ret = _pct(r["c0"], r["c1"]), _pct(r["c0"], r["c5"]), _pct(r["c0"], r["c21"])
            if not (_in_band(d_ret, DAY_BAND) and _in_band(w_ret, WEEK_BAND) and _in_band(m_ret, MONTH_BAND)):
                fails["returns"].append(sym)
                continue
            stage["returns"] += 1

            cmp_v, pp, s1, low3 = r["c0"], r["pp"], r["s1"], r["low3"]
            loc_ok = (cmp_v is not None and pp is not None and s1 is not None and low3 is not None
                      and float(cmp_v) >= float(pp) and float(low3) <= float(s1) * S1_TOUCH_TOL)
            if not loc_ok:
                fails["location"].append(sym)
                continue
            stage["location"] += 1

            legs, vdetail = _volume_legs(r)
            if legs < VOL_LEGS_NEEDED:
                fails["volume"].append(sym)
                continue
            stage["volume"] += 1

            qualified.append({
                "symbol": sym, "gvm": round(gvm, 2), "dgvm_90d": round(dgvm, 2),
                "segment": r["segment"], "cmp": float(cmp_v),
                "gates": {
                    "quality": {"gvm": round(gvm, 2), "dgvm_90d": round(dgvm, 2)},
                    "sector": sector_note,
                    "returns": {"day": round(d_ret, 2), "week": round(w_ret, 2), "month": round(m_ret, 2)},
                    "location": {"cmp": float(cmp_v), "pp": float(pp), "s1": float(s1),
                                 "low_%dd" % S1_TOUCH_SESSIONS: float(low3)},
                    "volume": {"legs_passed": legs, "needed": VOL_LEGS_NEEDED, "legs": vdetail},
                },
            })

        # Rank is the founder's: dGVM_90d descending decides who gets the day's slots.
        qualified.sort(key=lambda q: -q["dgvm_90d"])

        return {
            "scan_ts": _ist_now(), "universe": len(rows), "stages": stage,
            "fails": {k: v[:FAIL_LIST_CAP] for k, v in fails.items()},
            "fails_truncated": {k: max(0, len(v) - FAIL_LIST_CAP) for k, v in fails.items()},
            "qualified": qualified, "nifty_week": nifty_wk,
            "segments_with_average": len(sector_avg),
        }
    finally:
        if own:
            conn.close()


# ── persistence: the funnel ledger, then the book ────────────────────────────────────────────
def _write_funnel(cur, f, entered, notes=None):
    """One row per scan_date, written on EVERY run.

    ON CONFLICT UPDATE rather than skip: a re-run of the same session must correct its own row,
    not leave the first attempt's numbers standing as though nothing happened. The unique
    constraint on scan_date is what makes a day one row instead of a pile.
    """
    trunc = {k: v for k, v in f["fails_truncated"].items() if v}
    payload = dict(f["fails"])
    if trunc:
        # Counts are always exact; only the NAMED examples are capped. Say so in the row rather
        # than letting a reader think a stage killed exactly 40 names.
        payload["_truncated"] = trunc
    cur.execute("""
        INSERT INTO qsr_funnel_daily
            (scan_date, scan_ts, universe, s_quality, s_sector, s_returns, s_location, s_volume,
             qualified, entered, fails, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (scan_date) DO UPDATE SET
            scan_ts = EXCLUDED.scan_ts, universe = EXCLUDED.universe,
            s_quality = EXCLUDED.s_quality, s_sector = EXCLUDED.s_sector,
            s_returns = EXCLUDED.s_returns, s_location = EXCLUDED.s_location,
            s_volume = EXCLUDED.s_volume, qualified = EXCLUDED.qualified,
            entered = EXCLUDED.entered, fails = EXCLUDED.fails, notes = EXCLUDED.notes
    """, (f["scan_ts"].date(), f["scan_ts"], f["universe"],
          f["stages"]["quality"], f["stages"]["sector"], f["stages"]["returns"],
          f["stages"]["location"], f["stages"]["volume"],
          len(f["qualified"]), entered, json.dumps(payload), notes))


def _open_slots(cur):
    cur.execute("SELECT COUNT(*) FROM qsr_positions WHERE status = 'OPEN'")
    return MAX_OPEN - int(cur.fetchone()[0])


def _already_open(cur):
    cur.execute("SELECT symbol FROM qsr_positions WHERE status = 'OPEN'")
    return {r[0] for r in cur.fetchall()}


def run_qsr_scan(dry_run=False):
    """The nightly job. Scans, writes the funnel ledger, then opens what the caps allow.

    THE LEDGER IS WRITTEN EVEN WHEN NOTHING QUALIFIES, and even when dry_run stops the book from
    moving. That ordering is the point of the whole card: the record of what the market offered
    is not contingent on the engine having taken anything.
    """
    now = _ist_now()
    with _conn() as conn:
        f = scan(conn)
        with conn.cursor() as cur:
            held = _already_open(cur)
            slots = _open_slots(cur)

            # RANKED, THEN CAPPED, in that order. scan() already sorted by dGVM_90d descending,
            # which is the founder's rank key, so the day's three go to the three fastest-rising
            # names rather than to whoever the universe happened to list first.
            take, skipped = [], []
            for q in f["qualified"]:
                if q["symbol"] in held:
                    skipped.append((q["symbol"], "already open"))
                    continue
                if len(take) >= MAX_NEW_PER_DAY:
                    skipped.append((q["symbol"], "daily cap %d reached" % MAX_NEW_PER_DAY))
                    continue
                if len(take) >= slots:
                    skipped.append((q["symbol"], "book full at %d open" % MAX_OPEN))
                    continue
                take.append(q)

            entered = 0
            if not dry_run:
                for q in take:
                    px = float(q["cmp"])
                    if px <= 0:
                        skipped.append((q["symbol"], "no usable price"))
                        continue
                    qty = int(NOTIONAL // px)
                    if qty < 1:
                        # A 1L notional cannot buy one share of a very expensive name. That is a
                        # real outcome, not an error, and it is recorded rather than rounded up.
                        skipped.append((q["symbol"], "1L notional buys < 1 share at %.2f" % px))
                        continue
                    cur.execute("""
                        INSERT INTO qsr_positions
                            (symbol, entry_date, entry_ts, entry_price, qty, notional, stop_price,
                             gvm_at_entry, dgvm_90d, segment, gates, status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN')
                        ON CONFLICT (symbol, entry_date) DO NOTHING
                    """, (q["symbol"], now.date(), now, px, qty, qty * px,
                          round(px * (1 + HARD_STOP_PCT / 100.0), 2),
                          q["gvm"], q["dgvm_90d"], q["segment"], json.dumps(q["gates"])))
                    entered += cur.rowcount

            note = "dry_run" if dry_run else None
            if skipped:
                note = ((note + " | ") if note else "") + "skipped: " + "; ".join(
                    "%s (%s)" % s for s in skipped[:FAIL_LIST_CAP])
            _write_funnel(cur, f, entered, note)
        conn.commit()

    log.info("qsr scan %s: universe=%d quality=%d sector=%d returns=%d location=%d volume=%d "
             "qualified=%d entered=%d",
             now.date(), f["universe"], f["stages"]["quality"], f["stages"]["sector"],
             f["stages"]["returns"], f["stages"]["location"], f["stages"]["volume"],
             len(f["qualified"]), entered)
    return {"scan_date": str(now.date()), "universe": f["universe"], "stages": f["stages"],
            "qualified": [q["symbol"] for q in f["qualified"]],
            "entered": entered, "dry_run": dry_run,
            "skipped": [{"symbol": s, "why": w} for s, w in skipped]}


def run_qsr_exits():
    """Nightly exit sweep. Three reasons, no target — 27980 says winners run until one fires.

    Evaluated in severity order: a stop that was hit is the trade's outcome even if the quality
    also broke and the clock also ran out on the same day. Reporting the softest reason when the
    hardest one fired would misdescribe the loss.
    """
    now = _ist_now()
    closed = []
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, symbol, entry_date, entry_ts, entry_price, qty, stop_price, "
                    "gvm_at_entry, dgvm_90d, segment, gates FROM qsr_positions WHERE status='OPEN'")
        cols = [d[0] for d in cur.description]
        open_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not open_rows:
            return {"checked": 0, "closed": 0, "exits": []}

        syms = [r["symbol"] for r in open_rows]
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, close, price_date FROM raw_prices
                       WHERE symbol = ANY(%s) AND close IS NOT NULL
                       ORDER BY symbol, price_date DESC""", (syms,))
        px = {r[0]: (float(r[1]), r[2]) for r in cur.fetchall()}
        cur.execute("""SELECT symbol, gvm_score FROM gvm_scores
                       WHERE symbol = ANY(%s)
                         AND score_date = (SELECT MAX(score_date) FROM gvm_scores)""", (syms,))
        gvm = {r[0]: (float(r[1]) if r[1] is not None else None) for r in cur.fetchall()}

        for p in open_rows:
            last = px.get(p["symbol"])
            if last is None:
                continue                       # no price today: hold, never guess an exit
            price, pdate = last
            held = (pdate - p["entry_date"]).days
            g_now = gvm.get(p["symbol"])
            reason = None
            if price <= float(p["stop_price"]):
                reason = "HARD_STOP"
            elif g_now is not None and g_now < QUALITY_BREAK_GVM:
                reason = "QUALITY_BREAK"
            elif held >= TIME_STOP_SESS:
                reason = "TIME_STOP"
            if not reason:
                continue

            entry, qty = float(p["entry_price"]), int(p["qty"])
            pnl = (price - entry) * qty
            cur.execute("""
                INSERT INTO qsr_trades
                    (symbol, entry_date, entry_ts, entry_price, qty, exit_date, exit_ts,
                     exit_price, exit_reason, held_sessions, pnl, pnl_pct,
                     gvm_at_entry, gvm_at_exit, dgvm_90d, segment, gates)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (p["symbol"], p["entry_date"], p["entry_ts"], entry, qty, pdate, now,
                  price, reason, held, round(pnl, 2), round((price / entry - 1) * 100, 2),
                  p["gvm_at_entry"], g_now, p["dgvm_90d"], p["segment"],
                  json.dumps(p["gates"]) if isinstance(p["gates"], (dict, list)) else p["gates"]))
            cur.execute("UPDATE qsr_positions SET status='CLOSED' WHERE id=%s", (p["id"],))
            closed.append({"symbol": p["symbol"], "reason": reason,
                           "pnl_pct": round((price / entry - 1) * 100, 2)})
        conn.commit()
    log.info("qsr exits: checked=%d closed=%d", len(open_rows), len(closed))
    return {"checked": len(open_rows), "closed": len(closed), "exits": closed}
