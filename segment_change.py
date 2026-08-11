"""segment_change.py — cc#810 SEGMENT UNIFICATION (founder 02-Aug-2026).

ONE computation source for a segment's price change. Every surface that renders a sector/segment
move — the v8_metrics writer (sector_day/week/month), TC cards, the GVM page chips — reads it from
here. Same doctrine as cc#803 for the C button: if two surfaces compute the same number two ways,
they will eventually disagree, and the founder will be the one who finds out.

THE TAXONOMY IS gvm_scores.segment. The previous aggregation lived in
v8_signal_writer._update_sector_aggregates_sql and averaged v8_metrics rows — and v8_metrics only
holds the ~209 FUTURES symbols. So "the sector" meant "the two or three F&O names in the sector",
which is the APLAPOLLO distortion this task exists to kill. Measured on the 31-Jul session for
Pipes & Tubes (16 members, 15 priced):

    mcap-weighted, ALL members   -1.59%   <- what ships
    simple average, ALL members  +0.08%
    simple average, F&O ONLY     -2.74%   <- what shipped before, from 2 of 16 members

Three different stories from one segment on one day. The weighting matters as much as the
membership: APLAPOLLO alone is 31.6% of the segment's market cap, so an equal-weight average of the
full membership is just as wrong in the other direction.

THE FORMULA (founder rule 2): SUM(mcap_i * chg_i) / SUM(mcap_i), index-style, ALL members included.
Weights come from screener_raw.market_cap, which refreshes with the daily CSV.

PRICING (founder rule, cc#811 dependency): each member's current price is its live/last 5-min SPOT
tick where one exists, else its latest EOD close — the same ladder cmp_resolver applies, so a
segment aggregate can never disagree with the member prices printed next to it. Members are NOT
dropped for lacking a live feed; dropping them would silently re-introduce the futures-only bias
this task removes. A member with no usable price at all is excluded from both the numerator and the
denominator, so the weights still sum to 1 across whoever IS priced.
"""
import logging

log = logging.getLogger("segment_change")

# cc#811: SPOT sources only. 'fyers_fut' must never appear here — an F&O symbol carries an eq bar
# and a fut bar at the same ts, and the futures premium would enter the aggregate as if it were spot.
# cc#1011: a LIST (not a tuple) because the query below binds it to `= ANY(%(spot)s)` (psycopg3 array
# param), not the old `IN %(spot)s` — see the note on the query.
SPOT_SOURCES = ['fyers_eq', 'fyers_ext', 'fyers_hist']

# Calendar-day lookbacks for the week/month anchors. Deliberately calendar rather than trading-day
# counts: the anchor is "the last close at or before this date", so holidays and weekends resolve to
# the nearest prior session automatically instead of needing a trading-day calendar here.
WEEK_LOOKBACK_DAYS = 7
MONTH_LOOKBACK_DAYS = 30

# One pass. Per member: current price (live tick, else EOD close), the three lookback anchors, and
# the market-cap weight; then a weighted roll-up per segment.
_SEGMENT_SQL = """
WITH members AS (
    SELECT g.symbol, g.segment, NULLIF(sr.market_cap::numeric, 0) AS mcap
    FROM gvm_scores g
    LEFT JOIN screener_raw sr ON sr.nse_code = g.symbol
    WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
      AND g.segment IS NOT NULL AND g.segment <> ''
),
live AS (
    SELECT DISTINCT ON (i.symbol) i.symbol, i.close, i.ts::date AS px_date
    FROM intraday_prices i
    JOIN members m ON m.symbol = i.symbol
    -- cc#1011: `= ANY(%(spot)s)` not `IN %(spot)s`. Under psycopg3 (this codebase) a tuple param is
    -- NOT expanded into an IN-list the way psycopg2 did, so `i.source IN %(spot)s` raised every time
    -- this ran inside the signal-writer tick — silently, because segment_changes() swallowed it and
    -- returned {}. That is why cc#810's wide table-write never landed (the table stayed F&O for
    -- weeks) and why, once cc#1003 moved this call ahead of the metrics upsert, the poisoned tick
    -- transaction wrote 0/208. `= ANY(array)` is the correct psycopg3 pattern.
    WHERE i.source = ANY(%(spot)s) AND i.timeframe = '5m' AND i.close IS NOT NULL
    ORDER BY i.symbol, i.ts DESC
),
eod AS (
    SELECT DISTINCT ON (r.symbol) r.symbol, r.close, r.price_date AS px_date
    FROM raw_prices r
    JOIN members m ON m.symbol = r.symbol
    WHERE r.close IS NOT NULL
    ORDER BY r.symbol, r.price_date DESC
),
px AS (
    SELECT m.symbol, m.segment, m.mcap,
           COALESCE(l.close, e.close)     AS price,
           COALESCE(l.px_date, e.px_date) AS px_date,
           (l.close IS NOT NULL)          AS is_live
    FROM members m
    LEFT JOIN live l ON l.symbol = m.symbol
    LEFT JOIN eod  e ON e.symbol = m.symbol
),
anchored AS (
    SELECT p.*,
           (SELECT r.close FROM raw_prices r
             WHERE r.symbol = p.symbol AND r.close IS NOT NULL AND r.price_date < p.px_date
             ORDER BY r.price_date DESC LIMIT 1) AS base_d,
           (SELECT r.close FROM raw_prices r
             WHERE r.symbol = p.symbol AND r.close IS NOT NULL
               AND r.price_date <= p.px_date - %(wk)s
             ORDER BY r.price_date DESC LIMIT 1) AS base_w,
           (SELECT r.close FROM raw_prices r
             WHERE r.symbol = p.symbol AND r.close IS NOT NULL
               AND r.price_date <= p.px_date - %(mo)s
             ORDER BY r.price_date DESC LIMIT 1) AS base_m
    FROM px p
    WHERE p.price IS NOT NULL AND p.px_date IS NOT NULL AND p.mcap IS NOT NULL
),
chg AS (
    SELECT segment, symbol, mcap, is_live,
           CASE WHEN base_d > 0 THEN (price / base_d - 1) * 100 END AS chg_d,
           CASE WHEN base_w > 0 THEN (price / base_w - 1) * 100 END AS chg_w,
           CASE WHEN base_m > 0 THEN (price / base_m - 1) * 100 END AS chg_m
    FROM anchored
),
-- Total membership, INCLUDING members that could not be priced. The tooltip quotes this (the
-- segment really does have 16 members); the weighted maths necessarily runs on the 15 that have a
-- price. Reporting both is the point — a bare member count that silently means "the ones that
-- worked" is exactly the missing-denominator failure UNIVERSE_DENOMINATOR_RULE exists to prevent.
totals AS (SELECT segment, COUNT(*) AS members_total FROM members GROUP BY segment)
SELECT segment,
       ROUND((SUM(mcap * chg_d) FILTER (WHERE chg_d IS NOT NULL)
              / NULLIF(SUM(mcap) FILTER (WHERE chg_d IS NOT NULL), 0))::numeric, 2) AS seg_day,
       ROUND((SUM(mcap * chg_w) FILTER (WHERE chg_w IS NOT NULL)
              / NULLIF(SUM(mcap) FILTER (WHERE chg_w IS NOT NULL), 0))::numeric, 2) AS seg_week,
       ROUND((SUM(mcap * chg_m) FILTER (WHERE chg_m IS NOT NULL)
              / NULLIF(SUM(mcap) FILTER (WHERE chg_m IS NOT NULL), 0))::numeric, 2) AS seg_month,
       COUNT(*)                                        AS members_priced,
       COUNT(*) FILTER (WHERE is_live)                 AS members_live,
       (SELECT t.members_total FROM totals t WHERE t.segment = chg.segment) AS members_total
FROM chg
GROUP BY segment
"""


def _params():
    return {"spot": SPOT_SOURCES, "wk": WEEK_LOOKBACK_DAYS, "mo": MONTH_LOOKBACK_DAYS}


def segment_changes(cur):
    """{segment: {"day","week","month","members_priced","members_live"}} for every segment.

    One query for the whole taxonomy — it runs on every 5-min signal_writer tick, so a per-segment
    or per-symbol loop is not an option."""
    out = {}
    try:
        cur.execute(_SEGMENT_SQL, _params())
        for seg, d, w, m, n, nlive, ntot in cur.fetchall():
            out[seg] = {"day": float(d) if d is not None else None,
                        "week": float(w) if w is not None else None,
                        "month": float(m) if m is not None else None,
                        "members_priced": int(n or 0), "members_live": int(nlive or 0),
                        "members_total": int(ntot or n or 0)}
    except Exception as e:
        log.warning(f"segment_changes: {e}")
        # cc#1011: a failed query leaves the connection in an ABORTED transaction. Clear it so a
        # caller that keeps using this connection (the signal-writer tick, TC cards, GVM chips) does
        # not then die on "current transaction is aborted". Returning {} is the fail-closed contract;
        # leaving a poisoned connection behind was how one bad read killed a whole tick.
        try:
            cur.connection.rollback()
        except Exception:
            pass
    return out


def segment_change_for(cur, segment):
    """Single segment, same numbers. Convenience for card/chip renderers."""
    if not segment:
        return None
    return segment_changes(cur).get(segment)


# The label every surface should print alongside the number, so the denominator travels with it
# (UNIVERSE_DENOMINATOR_RULE, debug_learnings id=207) — "16 members · mcap-weighted", never a bare %.
def segment_label(info):
    if not info:
        return "Segment"
    total = info.get("members_total") or info.get("members_priced") or 0
    priced = info.get("members_priced") or 0
    base = f"Segment · {total} member{'' if total == 1 else 's'} · mcap-weighted"
    # Only surfaces the gap when there IS one, so the common case stays the clean founder-specified
    # string and the exception is never silent.
    return base if priced >= total else f"{base} ({priced} priced)"
