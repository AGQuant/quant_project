"""cc#674 + cc#680: Time-of-day-adjusted Relative Volume (RVOL) engine.

RVOL = today's cumulative volume up to the current 5-min slot / the 21-session AVERAGE cumulative
volume up to the SAME slot. A 1.4x RVOL at 10:30 means the symbol has already traded 1.4x the volume
it typically has by 10:30 — a pace read, not a full-day compare.

cc#680 CRITICAL FIX — intraday volume semantics. intraday_prices 5-min `volume` is stored CUMULATIVE
on most sessions (each bar = cumulative shares since open; the day's final bar ≈ the raw_prices day
volume) but PER-BAR on some. Naively SUM()-ing cumulative bars inflated the profile ~13-38x, so RVOL
read ~0.1x when the truth was ~1.0x. This is the same trap the v8_signal_writer VolX baseline already
handles (cc#150/cc#170 monotonic→cumulative detection). We now detect per session and anchor to the
raw_prices day volume (ground truth):
  * build_profiles() — per session, is_cumulative if MAX(bar) >= 0.3 * raw_prices day-volume (a single
    5-min bar is never 30% of a day unless it's a cumulative counter). cum(t) = bar-value if cumulative
    else running-sum; each session's curve is normalised to its raw day volume, then the whole per-slot
    average is scaled so the final slot = the symbol's 21d avg raw volume. Invariant: final slot must
    be within 0.5x–2x of the 21d raw avg, else the symbol is dropped + ops_log rvol_profile_mismatch.
  * live_rvol(cur, symbol) — today's cumulative volume is cumulative-aware (majority-non-decreasing →
    cumulative → latest bar; else SUM), divided by the profile at the current slot.
"""
import os
import logging
from datetime import datetime, timedelta

import psycopg

log = logging.getLogger("rvol_engine")
DATABASE_URL = os.getenv("DATABASE_URL", "")

SESSION_START = "09:15"
SESSION_END = "15:25"
EARLY_SLOTS = ("09:15:00", "09:20:00")
MIN_SESSIONS = 10

# cc#1649 P2 GATE FINDING (session_log, cc_task_logs task 1649): fyers_eq itself ends 15:10 on
# most symbol-days (some reach 15:15); the exchange's own closing-auction volume for 15:15-15:25
# is stored separately under source='fyers_eq_auction' (confirmed by sampling 20 symbols x 3
# completed sessions: SUM(fyers_eq bars, cumulative-aware) + SUM(fyers_eq_auction bars) landed
# within ~1.0-1.1x of raw_prices day volume on every clean day, vs 0.52x-2.35x for fyers_eq alone
# -- auction bars are PER-BAR and additive, never a continuation of the eq cumulative series).
# SECOND FINDING, not anticipated by the card: fyers_eq_auction bars are THEMSELVES posted
# irregularly per slot per day (same "not every 5-min slot every day" problem as eq's own tail),
# so naively unioning auction bars in as extra per-slot rows left sessions_used at the tail
# slots around 6-9, not ~21 -- it moved the starvation, it did not fix it. The three tail slots
# are instead generated EXPLICITLY per (symbol, day) that has valid eq session data, so every
# qualifying day contributes a row at 15:15/15:20/15:25 regardless of whether an auction bar
# happens to exist at that exact slot (0 auction-so-far is a valid, honest default, not a gap).
_TAIL_SLOTS = [
    (datetime.strptime(SESSION_END, "%H:%M") - timedelta(minutes=10)).strftime("%H:%M:00"),
    (datetime.strptime(SESSION_END, "%H:%M") - timedelta(minutes=5)).strftime("%H:%M:00"),
    datetime.strptime(SESSION_END, "%H:%M").strftime("%H:%M:00"),
]   # ["15:15:00", "15:20:00", "15:25:00"] as of SESSION_END="15:25" -- derived, not hardcoded twice

# cc#680: rebuild + anchor SQL (also used verbatim by the one-shot re-seed). Per-session cumulative
# detection via the raw_prices day-volume anchor; normalise each session to its raw day volume; scale
# the per-slot average so the 15:25 slot == the symbol's 21d avg raw volume.
#
# cc#1649 P2: eq(+hist) bars keep the ORIGINAL cumulative-aware detection and normalisation,
# unchanged, for every slot before the tail. The three tail slots (see _TAIL_SLOTS) add the
# fyers_eq_auction volume accumulated up to that slot on top of the eq series' own final
# cumulative value for that day -- cum(t) = eq_final_cum + SUM(auction bars at-or-before t).
_BUILD_SQL = """
WITH dedup AS (
  SELECT DISTINCT ON (symbol, ts) symbol, ts, volume, source FROM intraday_prices
  WHERE source IN ('fyers_eq','fyers_eq_auction','fyers_hist') AND ts::date >= CURRENT_DATE - 45
  ORDER BY symbol, ts, CASE source WHEN 'fyers_eq' THEN 0 WHEN 'fyers_eq_auction' THEN 1 ELSE 2 END),
eqbars AS (SELECT symbol, ts::date d, ts::time slot, volume,
                  SUM(volume) OVER (PARTITION BY symbol, ts::date ORDER BY ts) run_sum
           FROM dedup WHERE source != 'fyers_eq_auction'),
aucbars AS (SELECT symbol, ts::date d, ts::time slot, volume FROM dedup WHERE source = 'fyers_eq_auction'),
sess AS (SELECT b.symbol, b.d, MAX(b.volume) max_bar, MAX(b.run_sum) sum_bar, rp.volume raw_vol
         FROM eqbars b JOIN raw_prices rp ON rp.symbol=b.symbol AND rp.price_date=b.d
         GROUP BY b.symbol, b.d, rp.volume),
sess2 AS (SELECT symbol, d, raw_vol, (max_bar >= 0.3*raw_vol) is_cum,
                 CASE WHEN max_bar >= 0.3*raw_vol THEN max_bar ELSE sum_bar END day_max
          FROM sess WHERE raw_vol > 0),
eqfinal AS (SELECT b.symbol, b.d,
                   (CASE WHEN s.is_cum THEN MAX(b.volume) ELSE MAX(b.run_sum) END) eq_final_cum
            FROM eqbars b JOIN sess2 s ON s.symbol=b.symbol AND s.d=b.d
            GROUP BY b.symbol, b.d, s.is_cum),
cs_eq AS (SELECT b.symbol, b.d, b.slot,
              (CASE WHEN s.is_cum THEN b.volume ELSE b.run_sum END)::numeric/NULLIF(s.day_max,0)*s.raw_vol cum_norm
       FROM eqbars b JOIN sess2 s ON s.symbol=b.symbol AND s.d=b.d WHERE b.slot < %(tail_start)s),
cs_tail AS (SELECT s.symbol, s.d, t.slot,
         (COALESCE(ef.eq_final_cum,0) +
          COALESCE((SELECT SUM(a2.volume) FROM aucbars a2
                     WHERE a2.symbol=s.symbol AND a2.d=s.d AND a2.slot<=t.slot), 0)
         )::numeric / NULLIF(s.day_max,0) * s.raw_vol AS cum_norm
       FROM sess2 s CROSS JOIN unnest(%(tail_slots)s::time[]) AS t(slot)
       LEFT JOIN eqfinal ef ON ef.symbol=s.symbol AND ef.d=s.d),
cs AS (SELECT symbol, d, slot, cum_norm, DENSE_RANK() OVER (PARTITION BY symbol ORDER BY d DESC) rnk
       FROM (SELECT * FROM cs_eq UNION ALL SELECT * FROM cs_tail) u),
prof AS (SELECT symbol, slot, AVG(cum_norm) p, COUNT(DISTINCT d) n
         FROM cs WHERE rnk<=21 AND slot BETWEEN %(ss)s AND %(se)s GROUP BY symbol, slot),
raw21 AS (SELECT symbol, AVG(raw_vol) r FROM (SELECT DISTINCT symbol, d, raw_vol,
             DENSE_RANK() OVER (PARTITION BY symbol ORDER BY d DESC) rk FROM sess2) z WHERE rk<=21 GROUP BY symbol),
p1525 AS (SELECT symbol, p FROM prof WHERE slot=%(se_t)s)
INSERT INTO rvol_profiles (symbol, slot_time, avg_cum_vol, sessions_used, computed_at)
SELECT prof.symbol, prof.slot,
       GREATEST(ROUND(prof.p * COALESCE(raw21.r/NULLIF(p1525.p,0),1)),0),
       prof.n, NOW()
FROM prof JOIN raw21 ON raw21.symbol=prof.symbol LEFT JOIN p1525 ON p1525.symbol=prof.symbol
"""


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _is_market_hours(now=None):
    now = now or _ist_now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= hm <= (15 * 60 + 30)


def _ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS rvol_profiles (
        symbol TEXT NOT NULL, slot_time TIME NOT NULL,
        avg_cum_vol NUMERIC, sessions_used INT,
        computed_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY (symbol, slot_time))""")


def _ops_log(cur, category, title, details):
    try:
        import json
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)", (category, title, json.dumps(details)))
    except Exception:
        pass


def build_profiles(conn=None):
    """Nightly rebuild: cumulative-aware, raw-anchored per-slot average cumulative-volume profiles for
    every equity symbol with recent fyers_eq intraday. Full replace. Invariant 1: drop any symbol whose
    final-slot avg strays outside 0.5x–2x of its 21d raw avg (ops_log rvol_profile_mismatch).
    Invariant 2 (cc#1649 P3): drop any symbol whose SESSION_END slot itself is under MIN_SESSIONS
    (ops_log rvol_profile_anchor_thin) — a profile anchored on fewer than MIN_SESSIONS sessions is
    not a profile, even if its scaled final-slot average happens to pass invariant 1 by chance."""
    own = conn is None
    conn = conn or _conn()
    try:
        cur = conn.cursor()
        _ensure_table(cur)
        cur.execute("DELETE FROM rvol_profiles")
        cur.execute(_BUILD_SQL, {"ss": SESSION_START, "se": SESSION_END, "se_t": SESSION_END + ":00",
                                  "tail_start": _TAIL_SLOTS[0], "tail_slots": _TAIL_SLOTS})
        # invariant 1: final-slot avg must be within 0.5x–2x of the symbol's 21d avg raw volume.
        cur.execute("""
            WITH fin AS (SELECT DISTINCT ON (symbol) symbol, avg_cum_vol FROM rvol_profiles
                         ORDER BY symbol, slot_time DESC),
            raw21 AS (SELECT symbol, AVG(volume) r FROM (
                        SELECT symbol, volume, row_number() OVER (PARTITION BY symbol ORDER BY price_date DESC) rk
                        FROM raw_prices) z WHERE rk<=21 GROUP BY symbol)
            SELECT f.symbol, ROUND(f.avg_cum_vol), ROUND(r.r) FROM fin f JOIN raw21 r ON r.symbol=f.symbol
            WHERE r.r > 0 AND (f.avg_cum_vol < 0.5*r.r OR f.avg_cum_vol > 2.0*r.r)""")
        bad = cur.fetchall()
        for sym, fin_v, raw_v in bad:
            cur.execute("DELETE FROM rvol_profiles WHERE symbol=%s", (sym,))
            _ops_log(cur, "rvol", "rvol_profile_mismatch",
                     {"symbol": sym, "final_slot_avg": float(fin_v or 0), "raw21_avg": float(raw_v or 0)})
        # invariant 2 (cc#1649 P3): the SESSION_END slot's own sessions_used must clear MIN_SESSIONS.
        cur.execute("""SELECT symbol, sessions_used FROM rvol_profiles WHERE slot_time = %s
                       AND sessions_used < %s""", (SESSION_END + ":00", MIN_SESSIONS))
        thin = cur.fetchall()
        for sym, n in thin:
            cur.execute("DELETE FROM rvol_profiles WHERE symbol=%s", (sym,))
            _ops_log(cur, "rvol", "rvol_profile_anchor_thin",
                     {"symbol": sym, "session_end_sessions_used": int(n or 0), "min_sessions": MIN_SESSIONS})
        cur.execute("SELECT COUNT(DISTINCT symbol), COUNT(*) FROM rvol_profiles")
        nsym, nrows = cur.fetchone()
        conn.commit()
        dropped = len(bad) + len(thin)
        log.info(f"rvol_profiles: {nsym} symbols, {nrows} rows, {dropped} dropped "
                 f"({len(bad)} mismatch, {len(thin)} anchor_thin)")
        return {"symbols": nsym, "rows": nrows, "dropped": dropped,
                "dropped_mismatch": len(bad), "dropped_anchor_thin": len(thin)}
    finally:
        if own:
            conn.close()


def _cum_total(vols):
    """cc#680 cumulative-aware day total, extracted at cc#1440 so live_rvol, live_rvol_batch and
    closing_rvol_batch share ONE detection: a majority non-decreasing series is a cumulative
    counter → total = the latest (max) bar; else per-bar → total = SUM. Behaviour identical to the
    inline form live_rvol carried since cc#680."""
    if len(vols) >= 3:
        nondec = sum(1 for a, b in zip(vols, vols[1:]) if b >= a)
        if (nondec / max(len(vols) - 1, 1)) >= 0.6:
            return max(vols)
    return sum(vols)


def _mixed_total(vols, srcs):
    """cc#1649 P2: eq(+hist) bars keep the cc#680 cumulative-vs-per-bar detection unchanged;
    fyers_eq_auction bars are always per-bar (GATE finding, cc_task_logs task 1649) and are added
    on top via plain SUM, never blended into the eq series' own detection."""
    eq_v = [v for v, s in zip(vols, srcs) if s != "fyers_eq_auction"]
    auc_v = [v for v, s in zip(vols, srcs) if s == "fyers_eq_auction"]
    return _cum_total(eq_v) + sum(auc_v)


def live_rvol(cur, symbol):
    """On-demand RVOL. Today's cumulative volume is CUMULATIVE-AWARE (cc#680): if today's bars are a
    majority non-decreasing series they are a cumulative counter → today_cum = the latest bar; else
    per-bar → today_cum = SUM. Divided by the profile at the latest completed slot.

    cc#1649 P1/P2: the spec named _day_ratios AND live_rvol as sharing the starved-slot fallback
    (P1 shipped it to _day_ratios only — this closes that gap in the same push that adds the
    eq+auction mix). Walks back to the latest earlier slot whose profile clears MIN_SESSIONS when
    the last-bar slot itself doesn't; mixes fyers_eq_auction bars via _mixed_total. `slot`/`early`
    in the return reflect whichever slot the ratio actually used, honestly."""
    sym = (symbol or "").upper()
    cur.execute("""SELECT MAX(ts::date) FROM intraday_prices
                   WHERE symbol=%s AND source IN ('fyers_eq','fyers_eq_auction','fyers_hist')""", (sym,))
    r = cur.fetchone()
    asof = r[0] if r else None
    if not asof:
        return None
    cur.execute("""SELECT ts::time, volume, source FROM (
                     SELECT DISTINCT ON (ts) ts, volume, source FROM intraday_prices
                     WHERE symbol=%s AND source IN ('fyers_eq','fyers_eq_auction','fyers_hist') AND ts::date=%s
                     ORDER BY ts, CASE source WHEN 'fyers_eq' THEN 0 WHEN 'fyers_eq_auction' THEN 1 ELSE 2 END
                   ) q ORDER BY ts""", (sym, asof))
    bars = cur.fetchall()
    if not bars:
        return None
    slots_list = [b[0] for b in bars]
    vols_f = [float(b[1] or 0) for b in bars]
    srcs = [b[2] for b in bars]
    last_slot = slots_list[-1]
    cur.execute("SELECT slot_time, avg_cum_vol, sessions_used FROM rvol_profiles WHERE symbol=%s", (sym,))
    profs = {row[0]: (float(row[1]) if row[1] is not None else None, int(row[2] or 0))
             for row in cur.fetchall()}
    slot, avg, sess = last_slot, *profs.get(last_slot, (None, 0))
    use_vols, use_srcs = vols_f, srcs
    if not (avg and avg > 0 and sess >= MIN_SESSIONS):
        for i in range(len(slots_list) - 1, -1, -1):
            a, s = profs.get(slots_list[i], (None, 0))
            if a and a > 0 and s >= MIN_SESSIONS:
                slot, avg, sess = slots_list[i], a, s
                use_vols, use_srcs = vols_f[:i + 1], srcs[:i + 1]
                _ops_log(cur, "rvol", "rvol_slot_fallback",
                         {"symbol": sym, "day": str(asof), "from_slot": str(last_slot)[:8],
                          "to_slot": str(slot)[:8]})
                break
    cum_vol = _mixed_total(use_vols, use_srcs)
    slot_s = str(slot)
    now = _ist_now()
    closed = (not _is_market_hours(now)) or (asof != now.date())
    base = {"slot": slot_s[:5], "asof": str(asof), "closed": closed,
            "early": slot_s in EARLY_SLOTS, "sessions_used": sess}
    if not (avg and avg > 0 and sess >= MIN_SESSIONS):
        base["rvol"] = None
        base["insufficient"] = True
        return base
    base["rvol"] = round(cum_vol / avg, 2) if avg > 0 else None
    return base


# ── cc#1440 (VOLUME_METRICS_CANON_V2, session_log 33843) · batch reads ─────────────────────────
# RVOL and VOL P are ONE formula (cum volume at slot T / 21-session avg cum volume at slot T)
# read at two points: RVOL = today at the live slot, VOL P = the last COMPLETED session at its
# closing slot. cc#1649 P4: THE OLD CLAIM HERE ("15:25 — the final stored bucket") was never
# quite true and is corrected, not repeated — the read is whatever the last bar's slot actually
# is for that symbol-day (_day_ratios' MAX(ts)), same as it always was. Before cc#1649, that was
# usually 15:10 or 15:15 (fyers_eq's own tail — see the module's root-cause notes), so VOL P was
# quietly reading an EARLIER slot than the name promised. After cc#1649 P2, a day with any
# fyers_eq_auction activity extends the last bar to wherever the latest auction bar landed
# (typically 15:25, since that is where auction volume concentrates — see _TAIL_SLOTS), so VOL P
# now reaches the true 15:25 closing slot far more often — but it is still "whatever the last bar
# is", not a hardcoded 15:25, for the rare day with no auction bar at all. These batch forms live
# HERE, beside the profile math they read, so every display surface imports one derivation
# instead of growing its own copy (the Sprint A volume-flow SQL SUM is retired for this
# cumulative-aware read).

def _day_ratios(cur, symbols, day):
    """One query for `day`: per symbol, cumulative-aware total ÷ profile at that symbol's own
    last-bar slot. Returns {symbol: ratio-or-None}. MIN_SESSIONS gates sufficiency, same as
    live_rvol; missing bars/profile → None, never fabricated.

    cc#1649 P1 READ-SIDE FALLBACK: a starved close slot (fyers_eq often ends 15:10/15:15 on a
    given symbol-day, and the profile's own 15:15+ slots are themselves thin — see the module's
    root-cause notes) used to mean an automatic None even though the symbol has 20+ full sessions
    of history. If the profile at the last-bar slot is under MIN_SESSIONS, this walks BACK through
    that SAME symbol's own bars (already fetched, already ordered) to the latest earlier slot
    whose profile clears MIN_SESSIONS, and re-runs _cum_total on the truncated series (bars at or
    before that slot only) — the ratio becomes "pace up to that earlier slot", honestly, not a
    fabricated full-day number. No change to the stored profile, no change to MIN_SESSIONS.
    Returns None only when NO slot in the symbol's own bars — the last one or any earlier one —
    clears MIN_SESSIONS. Logs once per symbol-day to ops_log (category rvol, title
    rvol_slot_fallback) with from_slot/to_slot when the fallback actually fires, so the read
    stays honest and auditable rather than silently different from what a naive read would show.

    cc#1649 P2: bar read now also includes fyers_eq_auction (preference order fyers_eq ->
    fyers_eq_auction -> fyers_hist for a literal ts collision, same as _BUILD_SQL). Totals go
    through _mixed_total, same rule the profile build uses: cumulative-aware on the eq(+hist)
    portion, plain SUM of auction bars added on top."""
    cur.execute("""
        SELECT q.symbol, array_agg(q.ts::time ORDER BY q.ts) AS slots,
               array_agg(q.volume ORDER BY q.ts) AS vols,
               array_agg(q.source ORDER BY q.ts) AS srcs
        FROM (
            SELECT DISTINCT ON (symbol, ts) symbol, ts, volume, source FROM intraday_prices
            WHERE symbol = ANY(%s) AND source IN ('fyers_eq','fyers_eq_auction','fyers_hist') AND ts::date = %s
            ORDER BY symbol, ts, CASE source WHEN 'fyers_eq' THEN 0 WHEN 'fyers_eq_auction' THEN 1 ELSE 2 END
        ) q GROUP BY q.symbol
    """, (list(symbols), day))
    rows = cur.fetchall()
    if not rows:
        return {}
    cur.execute("""SELECT symbol, slot_time, avg_cum_vol, sessions_used FROM rvol_profiles
                   WHERE symbol = ANY(%s)""", ([r[0] for r in rows],))
    prof_by_sym = {}
    for sym, slot_time, avg_cum_vol, sessions_used in cur.fetchall():
        prof_by_sym.setdefault(sym, {})[slot_time] = (
            float(avg_cum_vol) if avg_cum_vol is not None else None, int(sessions_used or 0))
    out = {}
    for sym, slots_list, vols, srcs in rows:
        vols_f = [float(v or 0) for v in vols]
        profs = prof_by_sym.get(sym, {})
        last_slot = slots_list[-1]
        avg, sess = profs.get(last_slot, (None, 0))
        use_vols, use_srcs = vols_f, srcs
        if not (avg and avg > 0 and sess >= MIN_SESSIONS):
            fallback_slot = None
            for i in range(len(slots_list) - 1, -1, -1):
                a, s = profs.get(slots_list[i], (None, 0))
                if a and a > 0 and s >= MIN_SESSIONS:
                    fallback_slot, avg, sess = slots_list[i], a, s
                    use_vols, use_srcs = vols_f[:i + 1], srcs[:i + 1]
                    break
            if fallback_slot is None:
                out[sym] = None
                continue
            _ops_log(cur, "rvol", "rvol_slot_fallback",
                     {"symbol": sym, "day": str(day), "from_slot": str(last_slot)[:8],
                      "to_slot": str(fallback_slot)[:8]})
        total = _mixed_total(use_vols, use_srcs)
        out[sym] = round(total / avg, 2) if (avg and avg > 0 and total > 0) else None
    return out


def _sessions(cur, n=2):
    cur.execute("""SELECT DISTINCT ts::date AS d FROM intraday_prices
                   WHERE source = 'fyers_eq' ORDER BY d DESC LIMIT %s""", (n,))
    return [r[0] for r in cur.fetchall()]


def live_rvol_batch(cur, symbols):
    """{symbol: rvol-or-None} for the latest session — batch form of live_rvol's read."""
    days = _sessions(cur, 1)
    return _day_ratios(cur, symbols, days[0]) if days else {}


def day_rvol_batch(cur, symbols, day):
    """cc#1441: public batch read for an EXPLICIT session date — {symbol: ratio-or-None} at that
    day's last stored bar (for a completed day, the closing slot). Lets date-aware engines (the
    V8 bolt marker replaying a given date) read the same derivation as the live forms."""
    return _day_ratios(cur, symbols, day)


def closing_rvol_batch(cur, symbols):
    """VOL P (canon V2; day rule fixed cc#1449): {symbol: {'value': ratio, 'asof': 'YYYY-MM-DD'}
    or None} — the closing RVOL of the session immediately BEFORE the one live RVOL anchors to.
    live_rvol/live_rvol_batch anchor UNCONDITIONALLY to the latest intraday session (days[0]), so
    VOL P anchors UNCONDITIONALLY to days[1] — market open or closed. The pair must always be two
    DIFFERENT sessions; cc#1449's bug was a live/off-market branch here that collapsed both reads
    onto days[0] whenever the market was closed (every evening, weekend and holiday), so RVOL and
    VOL P printed identical numbers across every surface. {} only when history holds fewer than
    2 sessions — an honesty gate, never a market-hours one."""
    days = _sessions(cur, 2)
    if len(days) < 2:
        return {}
    day = days[1]
    ratios = _day_ratios(cur, symbols, day)
    return {s: ({"value": r, "asof": str(day)} if r is not None else None)
            for s, r in ratios.items()}


def closing_rvol(cur, symbol):
    """Single-symbol VOL P — thin wrapper over the batch read (one derivation)."""
    return closing_rvol_batch(cur, [symbol]).get((symbol or "").upper())


# ── cc#1441 · EOD full-universe form (VOLUME_METRICS_CANON_V2, session_log 33843) ──────────────
# The profile build ANCHORS the 15:25-slot average to the symbol's 21-session average raw volume
# (_BUILD_SQL: prof scaled by raw21/p1525). So the CLOSING read of the one formula is, by that
# anchoring, algebraically identical to: raw day volume ÷ trailing-21-session average raw volume
# (excluding the day itself) — readable straight from raw_prices for the whole ~1,800-symbol
# universe, no profile row needed. EOD gates (the V13 preset OR-gate, R6/R7's VOL P side) read
# THIS form; live intraday pace reads stay on the profile form above. Both forms live in this
# file so no gate or surface grows its own copy of the derivation.
#
# In the raw form, the latest raw_prices row is by definition the last COMPLETED session, so
# rvol = that session's closing RVOL and vol_p = the session before it — the (RVOL, VOL P) pair
# as an EOD gate sees it.
EOD_MIN_SESSIONS = 15   # history floor for the raw window — same honesty role as MIN_SESSIONS

# Self-contained (no params): per symbol, the latest completed session's closing RVOL (rvol)
# and the prior session's (vol_p). 60 calendar days ≈ 40 sessions, so the 21-back window behind
# the rn=1 row is always fully populated when the symbol has the history at all.
EOD_RVOL_PAIR_SQL = f"""
SELECT symbol, rv AS rvol, vp AS vol_p, price_date AS asof FROM (
  SELECT symbol, price_date,
         CASE WHEN a21 > 0 AND n21 >= {EOD_MIN_SESSIONS} THEN vol / a21 END AS rv,
         LAG(CASE WHEN a21 > 0 AND n21 >= {EOD_MIN_SESSIONS} THEN vol / a21 END)
           OVER (PARTITION BY symbol ORDER BY price_date) AS vp,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
  FROM (
    SELECT symbol, price_date, volume::numeric AS vol,
           AVG(volume::numeric) OVER (PARTITION BY symbol ORDER BY price_date
                                      ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING) AS a21,
           COUNT(volume)        OVER (PARTITION BY symbol ORDER BY price_date
                                      ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING) AS n21
    FROM raw_prices
    WHERE price_date >= CURRENT_DATE - 60 AND volume IS NOT NULL
  ) b
) z WHERE rn = 1
"""


def eod_rvol_pair(cur, symbol):
    """Single-symbol EOD read: {'rvol','vol_p','asof','prev_asof'} or None. Same raw form as
    EOD_RVOL_PAIR_SQL, scoped to one symbol (cheap: one symbol's 60-day slice). cc#1449 adds
    prev_asof (the prior session's date) so a caller pairing against a LIVE anchor can label
    the vol_p side honestly."""
    sym = (symbol or "").upper()
    cur.execute(f"""
        SELECT rv, vp, price_date, prev_d FROM (
          SELECT price_date,
                 CASE WHEN a21 > 0 AND n21 >= {EOD_MIN_SESSIONS} THEN vol / a21 END AS rv,
                 LAG(CASE WHEN a21 > 0 AND n21 >= {EOD_MIN_SESSIONS} THEN vol / a21 END)
                   OVER (ORDER BY price_date) AS vp,
                 LAG(price_date) OVER (ORDER BY price_date) AS prev_d,
                 ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
          FROM (
            SELECT price_date, volume::numeric AS vol,
                   AVG(volume::numeric) OVER (ORDER BY price_date
                                              ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING) AS a21,
                   COUNT(volume)        OVER (ORDER BY price_date
                                              ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING) AS n21
            FROM raw_prices
            WHERE symbol = %s AND price_date >= CURRENT_DATE - 60 AND volume IS NOT NULL
          ) b
        ) z WHERE rn = 1""", (sym,))
    r = cur.fetchone()
    if not r or (r[0] is None and r[1] is None):
        return None
    return {"rvol": (round(float(r[0]), 2) if r[0] is not None else None),
            "vol_p": (round(float(r[1]), 2) if r[1] is not None else None),
            "asof": str(r[2]),
            "prev_asof": (str(r[3]) if r[3] is not None else None)}


def eod_volume_ratio(cur, symbol):
    """cc#1631: the END-OF-DAY volume ratio for ANY symbol raw_prices carries - the latest session's
    volume over the trailing 21-session average (that session excluded from its own average),
    exactly the EOD_RVOL_PAIR_SQL derivation above, so the fallback and the futures Vol P canon are
    ONE formula. This answers "was volume high on the last session", never "is it high right now":
    callers must label it as EOD, not as RVOL TODAY. Returns {'ratio','asof','window','basis'} or
    None when the symbol has fewer than EOD_MIN_SESSIONS sessions of volume in the window."""
    pair = eod_rvol_pair(cur, symbol)
    if not pair or pair.get("rvol") is None:
        return None
    return {"ratio": pair["rvol"], "asof": pair["asof"], "window": 21,
            "basis": "raw_prices volume, latest session vs trailing 21-session average"}

