"""
qb_universe_builder.py -- cc#2123 V1: the Quant Basket Universe builder.

Founder, 16-Sep-2026: "Universe filter should be full page category wise... You are
building a Quant Basket Engine not screener." A screener answers what passes today;
an engine must be able to answer what passed on a past date and get the same list
back every time -- so every filter here is judged on whether it is POINT-IN-TIME
REPRODUCIBLE, not just whether it is available.

This is a genuinely new, separate page and API -- NOT an edit to v12_endpoints.py /
scorr_v12.html (that file's own do_not_touch stands; this card supersedes only its
"ship no code" framing, per FOUNDER_RULING_16SEP_STOP_ASKING_BUILD_IT, not its
file-level boundaries). New feature = own file (rule 5).

V1 SCOPE, stated before writing this file and shipped exactly as stated (cc_task_logs
task 2123): a POOL selector (Cap band Large/Mid/Small/Micro, All F&O, Nifty 500
top-500-mcap proxy -- radio, one at a time, chosen first per the founder's own words)
plus CATEGORY 1 (GVM and component scores: gvm/g/v/m_score, verdict, rank-within-
segment) as real, working, addable filters -- the founder's own explicitly named
starting point ("It is rank within a segment... build on that only and just build
that first"), and the one category proven to need ZERO new tables or roll-up jobs
(window functions over gvm_scores only, verified live on real data this session).
Categories 2-7 (40-filter register in the card spec) are NOT wired in this push --
they render as visible, named, "coming" sections on the page (never hidden -- the
founder must see the shape of the page he is maturing) and are explicit follow-up
scope, not a silent gap.

cc#2125 cutover: the four cap-band pools and the Nifty 500 pool now read mcap_rank_daily
(the per-date market-cap rank computed from screener_raw on every screener CSV load --
session_log 85's own named source), at its latest rank_date. Before this they read
input_raw.cap_category (frozen since June 2026) and nifty500_universe (one build,
2026-06-02) -- 111 companies sat in the wrong band on the 16-Sep-2026 batch. Every
rank-derived pool now states its ranking date on the page, and shows a stale badge only
when a newer screener batch exists that has not been ranked, or the rank is more than
two weeks old. nifty500_universe is retired as this page's source (not dropped -- other
readers are a separate card).
"""
import os
from datetime import date
from typing import Optional, List

import psycopg
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


# ── pool definitions ──────────────────────────────────────────────────────────────────────────
# Each pool resolves to a bare "SELECT symbol ..." used as a CTE. Symbol columns differ per
# source table (mcap_rank_daily.symbol, futures_universe.symbol) -- the SQL below aliases every one
# to `symbol` so the caller never needs to know which table it came from.
# cc#2125: cap bands and Nifty 500 read the LATEST rank_date of mcap_rank_daily (per-date rank from
# screener_raw, session_log 85 band edges applied at write time) -- never input_raw's frozen copy.
_CAP_BANDS = {
    "cap_large": ("Large Cap", "large"),
    "cap_mid": ("Mid Cap", "mid"),
    "cap_small": ("Small Cap", "small"),
    "cap_micro": ("Micro Cap", "micro"),
}

_RANK_LATEST = "rank_date = (SELECT MAX(rank_date) FROM mcap_rank_daily)"

_POOL_SQL = {
    "cap_large": f"SELECT symbol FROM mcap_rank_daily WHERE {_RANK_LATEST} AND cap_category = 'large'",
    "cap_mid": f"SELECT symbol FROM mcap_rank_daily WHERE {_RANK_LATEST} AND cap_category = 'mid'",
    "cap_small": f"SELECT symbol FROM mcap_rank_daily WHERE {_RANK_LATEST} AND cap_category = 'small'",
    "cap_micro": f"SELECT symbol FROM mcap_rank_daily WHERE {_RANK_LATEST} AND cap_category = 'micro'",
    "fo": "SELECT symbol FROM futures_universe WHERE is_active = true",
    "nifty500": f"SELECT symbol FROM mcap_rank_daily WHERE {_RANK_LATEST} AND mcap_rank <= 500",
}
_POOL_LABEL = {
    "cap_large": "Large Cap", "cap_mid": "Mid Cap", "cap_small": "Small Cap", "cap_micro": "Micro Cap",
    "fo": "All F&O Stocks", "nifty500": "Nifty 500 (top-500 mcap)",
}


_POOL_GROUP = {"cap_large": "cap_band", "cap_mid": "cap_band", "cap_small": "cap_band",
               "cap_micro": "cap_band", "fo": "fo", "nifty500": "nifty500"}


# cc#2143: MULTI-SELECT pools. The page sends the selection as repeated `pools=` query params
# (`pools=cap_large&pools=nifty500`); a comma-separated value inside one param is tolerated, and the
# legacy single `pool=` is folded in so nothing that ever held a link to this endpoint breaks (its
# only caller is scorr_qb_universe.html -- re-grepped for this card, still true). Order is kept,
# duplicates dropped.
def _pool_keys(pools, pool):
    keys = []
    for v in list(pools or []) + ([pool] if pool else []):
        for k in str(v).split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


# cc#2143: the CAT_1 population of several pools is the DISTINCT union of each pool's own
# _POOL_SQL -- never a sum of counts and never a plain concatenation, because the pools overlap
# (Nifty 500 = mcap_rank <= 500 contains all of Large Cap and part of Mid/Small; F&O can hold any
# cap size). Measured live before this shipped: Large Cap + Nifty 500 scored = 490 (the solo
# Nifty 500 count), not 100 + 490 = 590; F&O + Mid Cap = 264, not 352. One key = that pool's own
# SQL unchanged, so a single selection behaves byte-for-byte as before.
def _pool_union_sql(keys):
    if len(keys) == 1:
        return _POOL_SQL[keys[0]]
    return ("SELECT DISTINCT symbol FROM ("
            + " UNION ALL ".join("(" + _POOL_SQL[k] + ")" for k in keys) + ") u")


@router.get("/api/qb/universe2/pools")
def qb_universe2_pools():
    """Every pool's live count. Cap bands read mcap_rank_daily.cap_category at its latest rank_date
    (cc#2125: session_log 85's own locked band edges, applied when the rank is written from
    screener_raw on each CSV load -- this endpoint reads the classification, it does not re-derive
    it). F&O reads the registry (is_active), same convention every other surface in this codebase
    uses for the active futures list. Nifty 500 is OUR OWN top-500-by-market-cap (founder ruling,
    cc#2123: "That is our own calculation") -- mcap_rank <= 500 on the same latest rank_date -- not
    real NSE index membership, labelled as such. Every rank-derived pool carries `ranked_as_of` and
    says it in `note`; `stale` is set only when a newer screener batch has not been ranked yet or
    the rank is more than two weeks old (the failure class this card found), never by default.

    cc#2123: every pool's RAW size can differ from its SCORED size (how many of its members carry
    a current gvm_scores row) -- measured live this session, not assumed: F&O 208 raw / 205 scored
    (3 are index futures, BANKNIFTY/NIFTY/NIFTY50, GVM does not apply to an index), micro-cap 761/
    732, small-cap 748/744, Nifty 500 500/497. Large and mid have zero gap. The count shown and
    returned is always the SCORED count -- it is the one CAT_1 filtering can actually act on, and
    it is what must agree with /preview's own pool_count with zero filters applied. The raw count
    and the reason for any gap ride along in `note` so nothing is silently dropped unexplained."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(rank_date) FROM mcap_rank_daily")
        rank_date = cur.fetchone()[0]
        cur.execute("SELECT MAX(loaded_at) FROM screener_raw")
        scr_loaded = cur.fetchone()[0]
        raw = {}
        scored = {}
        for key, sql in _POOL_SQL.items():
            cur.execute("SELECT COUNT(*) FROM (" + sql + ") p")
            raw[key] = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM (" + sql + ") p WHERE EXISTS "
                "(SELECT 1 FROM gvm_scores g WHERE g.symbol = p.symbol "
                "AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores))")
            scored[key] = cur.fetchone()[0]

    # cc#2125: the rank is stale in exactly two honest cases -- a screener CSV landed and was not
    # ranked (the hook failed), or no CSV has landed for over two weeks (weekly cadence missed).
    # Neither is assumed; both are measured here on every call.
    rank_stale, rank_stale_note = False, None
    if rank_date is None:
        rank_stale = True
        rank_stale_note = "no market-cap rank has been written yet -- these pools are empty until the first screener load is ranked"
    elif scr_loaded is not None and scr_loaded.date() > rank_date:
        rank_stale = True
        rank_stale_note = (f"a screener CSV loaded {scr_loaded.date()} has not been ranked yet -- "
                           f"rank is still as of {rank_date}")
    elif (date.today() - rank_date).days > 14:
        rank_stale = True
        rank_stale_note = (f"ranked as of {rank_date}, more than two weeks ago -- no weekly screener "
                           "CSV has been loaded since")

    pools = []
    for key in _POOL_SQL:
        gap = raw[key] - scored[key]
        note = None
        if gap:
            reason = (" (BANKNIFTY/NIFTY/NIFTY50, no GVM score applies)" if key == "fo" else "")
            note = (f"{raw[key]} in the raw pool; {gap} have no current GVM score{reason} -- "
                    f"count shown is the {scored[key]} CAT_1 filters can actually act on")
        entry = {"key": key, "label": _POOL_LABEL[key], "group": _POOL_GROUP[key],
                 "count": scored[key], "raw_count": raw[key], "note": note}
        if _POOL_GROUP[key] in ("cap_band", "nifty500"):
            entry["ranked_as_of"] = str(rank_date) if rank_date else None
            asof = (f"ranked as of {rank_date} (screener CSV load)" if rank_date
                    else "no market-cap rank written yet")
            entry["note"] = asof + (f"; {note}" if note else "")
            if rank_stale:
                entry["stale"] = True
                entry["built_at"] = str(rank_date) if rank_date else None
                entry["stale_note"] = rank_stale_note
        pools.append(entry)
    return {"pools": pools}


# ── CAT_1: GVM and component scores -- the only category wired to real filtering in V1 ─────────
# gvm/g/v/m_score and verdict are direct gvm_scores columns (LIVE per the card's own register).
# seg_rank (rank within segment, by gvm_score) is the founder's own named starting point --
# verified live this session as a plain window function, no new table: RANK() OVER (PARTITION BY
# segment ORDER BY gvm_score DESC). seg_size and gap_vs_sector ride along in the same pass since
# the founder's own amendment (FOUNDER_AMENDMENT_16SEP_RANK_IS_SEGMENT_ONLY) requires the segment
# size to be visible beside the rank filter, not auto-applied as a second filter.
# cc#2145: the SECTOR metrics are computed over the WHOLE scored universe at the latest score_date --
# NOT over the pool -- so the number on this page is the number /sector shows and the one
# N5_SEGMENT_GATE_V1 gates on, whatever pool is picked. Basis mirrors compute_sector_ratings
# (gvm_nightly.py) exactly: gvm_score NOT NULL rows only; segment '', 'Unknown' and NULL skipped;
# mcap-weighted mean over members WITH a market cap (cc#1104: a missing market cap is an exclusion
# from the weight, never a fake weight); a segment where nobody has a market cap falls back to the
# simple mean; rounded to 3 dp like sector_ratings.mcap_weighted_gvm. sector_rank is DENSE_RANK over
# segments by the unrounded weighted mean. Window functions in-query, no table, no job (ruling 2).
_UNI_SECT_SQL = """
latest AS (SELECT MAX(score_date) AS d FROM gvm_scores),
uni AS (
    SELECT g.segment,
           SUM(CASE WHEN g.market_cap IS NOT NULL THEN g.gvm_score * g.market_cap END) AS w_num,
           SUM(CASE WHEN g.market_cap IS NOT NULL THEN g.market_cap END)               AS w_den,
           AVG(g.gvm_score) AS simple_avg,
           COUNT(*) AS members
      FROM gvm_scores g, latest
     WHERE g.score_date = latest.d AND g.gvm_score IS NOT NULL
       AND g.segment IS NOT NULL AND g.segment NOT IN ('', 'Unknown')
     GROUP BY g.segment
),
sect AS (
    SELECT segment, members AS sector_member_count,
           CASE WHEN w_den > 0 THEN w_num / w_den ELSE simple_avg END AS sector_gvm_raw
      FROM uni
),
sect_ranked AS (
    SELECT segment, sector_member_count, sector_gvm_raw,
           ROUND(sector_gvm_raw::numeric, 3) AS sector_gvm,
           DENSE_RANK() OVER (ORDER BY sector_gvm_raw DESC NULLS LAST) AS sector_rank
      FROM sect
)"""

# cc#2146: CAT_4 Change & Delta -- gvm_history ONLY (the point-in-time table), self-joined at date
# offsets. OFFSET VALUE = the symbol's LATEST row at score_date <= D - N days (an as-of lookup, never
# an exact-date match), and it must sit within ASOF_GRACE_DAYS of the offset: a row from a year ago
# is not "180 days ago", so it resolves as ABSENT (NULL -> excluded, cc#1822), which is what makes
# the 180d option honestly empty until daily history reaches it. Snapshot windows (verdict
# migration, consistency) count DISTINCT score_dates, never days -- 90 snapshots in the last 90d
# today vs ~4 a year before 2026. Verdict ordinal: Weak < Average < Good < Excellent
# (gvm_nightly._verdict). Same single query as CAT_1/CAT_2; index-bound on
# gvm_history (symbol, score_date DESC); no job, no cache.
ASOF_GRACE_DAYS = 45

_ORD = "CASE {v} WHEN 'Weak' THEN 1 WHEN 'Average' THEN 2 WHEN 'Good' THEN 3 WHEN 'Excellent' THEN 4 END"

def _asof_lateral(alias, days, cols="h.score_date, h.gvm_score, h.m_score, h.g_score, h.v_score"):
    return ("      LEFT JOIN LATERAL (SELECT " + cols + " FROM gvm_history h"
            " WHERE h.symbol = g.symbol AND h.score_date <= (SELECT d FROM latest) - " + str(int(days)) +
            " AND h.score_date >= (SELECT d FROM latest) - " + str(int(days)) + " - %(asof_grace)s"
            " ORDER BY h.score_date DESC LIMIT 1) " + alias + " ON true\n")

# universe-wide sector GVM at the 90d offset. gvm_history carries NO market cap and no table keeps
# one at past dates (mcap_rank_daily holds a single rank_date), so the offset rows are weighted with
# TODAY's market caps over the members that have a 90d row -- constant weights, so the change is a
# pure score movement. Stated on the card as a deviation from "same expression as cc#2145".
_UNI90_SQL = """
uni90 AS (
    SELECT g.segment,
           SUM(CASE WHEN g.market_cap IS NOT NULL THEN h90.gvm_score * g.market_cap END) AS w_num,
           SUM(CASE WHEN g.market_cap IS NOT NULL AND h90.gvm_score IS NOT NULL THEN g.market_cap END) AS w_den,
           AVG(h90.gvm_score) AS simple_avg,
           COUNT(h90.gvm_score) AS members_90d
      FROM gvm_scores g
""" + _asof_lateral("h90", 90, "h.gvm_score") + """     WHERE g.score_date = (SELECT d FROM latest) AND g.gvm_score IS NOT NULL
       AND g.segment IS NOT NULL AND g.segment NOT IN ('', 'Unknown')
     GROUP BY g.segment
),
sect90 AS (
    SELECT segment, members_90d,
           CASE WHEN w_den > 0 THEN w_num / w_den ELSE simple_avg END AS sector_gvm_90d_raw
      FROM uni90
)"""

# The ONE scored CTE both the preview rows and every count (alone / step / total) read from: the
# CAT_1 columns exactly as cc#2123 wrote them (seg_rank, seg_size, gap_vs_sector are POOL-scoped,
# the founder's own rank-within-segment), plus the cc#2145 universe-wide sector columns joined by
# segment. One query shape, one set of rows, so a count and a row can never disagree.
_SCORED_CTE = """
WITH pool AS ({pool_sql}),
""" + _UNI_SECT_SQL + """,
""" + _UNI90_SQL + """,
scored AS (
    SELECT g.symbol, g.company_name, g.segment, g.gvm_score, g.g_score, g.v_score, g.m_score,
           g.verdict, g.market_cap, g.price,
           RANK() OVER (PARTITION BY g.segment ORDER BY g.gvm_score DESC) AS seg_rank,
           COUNT(*) OVER (PARTITION BY g.segment) AS seg_size,
           ROUND((g.gvm_score - AVG(g.gvm_score) OVER (PARTITION BY g.segment))::numeric, 2) AS gap_vs_sector,
           s.sector_gvm, s.sector_rank, s.sector_member_count,
           ROUND((g.gvm_score - s.sector_gvm)::numeric, 2) AS gvm_minus_sector,
           -- cc#2146 CAT_4: as-of deltas (NULL = no row within the grace window = ABSENT)
           h30.score_date AS asof_30d, h90.score_date AS asof_90d, h180.score_date AS asof_180d,
           ROUND((g.gvm_score - h30.gvm_score)::numeric, 2)  AS gvm_change_30d,
           ROUND((g.gvm_score - h90.gvm_score)::numeric, 2)  AS gvm_change_90d,
           ROUND((g.gvm_score - h180.gvm_score)::numeric, 2) AS gvm_change_180d,
           ROUND((g.m_score - h30.m_score)::numeric, 2) AS m_change_30d,
           ROUND((g.m_score - h90.m_score)::numeric, 2) AS m_change_90d,
           ROUND((g.g_score - h90.g_score)::numeric, 2) AS g_change_90d,
           ROUND((g.v_score - h90.v_score)::numeric, 2) AS v_change_90d,
           ROUND((s.sector_gvm_raw - s9.sector_gvm_90d_raw)::numeric, 3) AS sector_gvm_change_90d,
           hm.verdict AS verdict_then,
           CASE WHEN hm.verdict IS NULL THEN NULL
                WHEN (""" + _ORD.format(v="g.verdict") + """) > (""" + _ORD.format(v="hm.verdict") + """) THEN 'upgraded'
                WHEN (""" + _ORD.format(v="g.verdict") + """) < (""" + _ORD.format(v="hm.verdict") + """) THEN 'downgraded'
                ELSE 'unchanged' END AS verdict_migration,
           CASE WHEN hc.n >= %(cons_n)s THEN hc.pos END AS gvm_pos_snapshots
      FROM gvm_scores g
      JOIN pool p ON p.symbol = g.symbol
      LEFT JOIN sect_ranked s ON s.segment = g.segment
      LEFT JOIN sect90 s9 ON s9.segment = g.segment
""" + _asof_lateral("h30", 30) + _asof_lateral("h90", 90) + _asof_lateral("h180", 180) + """      LEFT JOIN LATERAL (SELECT h.verdict FROM gvm_history h
                          WHERE h.symbol = g.symbol AND h.score_date < (SELECT d FROM latest)
                          ORDER BY h.score_date DESC OFFSET %(migr_n)s - 1 LIMIT 1) hm ON true
      LEFT JOIN LATERAL (SELECT COUNT(*) FILTER (WHERE z.dlt > 0) AS pos, COUNT(z.dlt) AS n
                           FROM (SELECT w.gvm_score - LEAD(w.gvm_score) OVER (ORDER BY w.score_date DESC) AS dlt
                                   FROM (SELECT h.score_date, h.gvm_score FROM gvm_history h
                                          WHERE h.symbol = g.symbol AND h.score_date <= (SELECT d FROM latest)
                                          ORDER BY h.score_date DESC LIMIT %(cons_n)s + 1) w) z) hc ON true
     WHERE g.score_date = (SELECT d FROM latest)
)
"""
_CAT1_SQL = _SCORED_CTE + """SELECT * FROM scored WHERE 1=1{where}
 ORDER BY gvm_score DESC NULLS LAST
 LIMIT %(limit)s
"""


@router.get("/api/qb/universe2/preview")
def qb_universe2_preview(
    pools: Optional[List[str]] = Query(None),
    pool: Optional[str] = None,
    gvm_min: Optional[float] = None, gvm_max: Optional[float] = None,
    g_min: Optional[float] = None, g_max: Optional[float] = None,
    v_min: Optional[float] = None, v_max: Optional[float] = None,
    m_min: Optional[float] = None, m_max: Optional[float] = None,
    verdict: Optional[List[str]] = Query(None),
    seg_rank_min: Optional[int] = None, seg_rank_max: Optional[int] = None,
    sector_gvm_min: Optional[float] = None, sector_gvm_max: Optional[float] = None,
    sector_rank_min: Optional[int] = None, sector_rank_max: Optional[int] = None,
    sector_gap_min: Optional[float] = None, sector_gap_max: Optional[float] = None,
    sector_members_min: Optional[int] = None, sector_members_max: Optional[int] = None,
    segments: Optional[List[str]] = Query(None),
    gvm_change_min: Optional[float] = None, gvm_change_max: Optional[float] = None, gvm_change_days: int = 30,
    m_change_min: Optional[float] = None, m_change_max: Optional[float] = None, m_change_days: int = 30,
    g_change_min: Optional[float] = None, g_change_max: Optional[float] = None,
    v_change_min: Optional[float] = None, v_change_max: Optional[float] = None,
    sector_change_min: Optional[float] = None, sector_change_max: Optional[float] = None,
    verdict_migration: Optional[List[str]] = Query(None), verdict_migration_n: int = 5,
    gvm_consistency_k: Optional[int] = None, gvm_consistency_n: int = 5,
    ops: Optional[str] = None,
    limit: int = 500,
):
    """cc#2123: pool(s) -> optional CAT_1 filters -> count + ranked rows. cc#2143: several pools
    may be selected (repeated `pools=`); their population is the DISTINCT union (see
    _pool_union_sql), pool_count is the SCORED size of that union. Every value read straight
    off gvm_scores' own current snapshot (score_date = MAX) -- no intraday feed anywhere on this
    path, per the founder's separate EOD-basis ruling on this same card (there is no live-price
    join here at all to violate it). filters_applied + binding_filter let the caller show WHY a
    count is what it is, per the card's own click-through requirement.

    cc#2135: conditions are built PER FILTER ROW (a min and a max on the same score are one filter,
    ANDed inside their own parentheses) because the page now applies, counts and combines whole
    rows. `per_filter_counts` is each applied filter ALONE against the pool (the page's "N of M
    pass" per row). `ops` = "verdict:OR,seg_rank:AND" gives each filter its own AND/OR; rows are
    combined LEFT-TO-RIGHT in the page's fixed row order (_FILTER_ORDER) with explicit parentheses
    at every step -- ((f1 OP f2) OP f3) -- the spec's stated default, flagged there for correction.
    The first applied row's flag has nothing to its left and is ignored. Omitting `ops` means AND
    everywhere, which is exactly what this endpoint did before this card."""
    keys = _pool_keys(pools, pool)
    if not keys:
        return {"error": "select at least one pool", "known": sorted(_POOL_SQL)}
    unknown = [k for k in keys if k not in _POOL_SQL]
    if unknown:
        return {"error": "unknown pool: " + ", ".join(unknown), "known": sorted(_POOL_SQL)}
    pool_sql = _pool_union_sql(keys)
    limit = max(1, min(int(limit), 2000))

    fconds, params, applied = {}, {}, []

    def rng(col, lo, hi, name):
        parts = []
        if lo is not None:
            parts.append(f"{col} >= %({name}_lo)s"); params[name + "_lo"] = lo
        if hi is not None:
            parts.append(f"{col} <= %({name}_hi)s"); params[name + "_hi"] = hi
        if parts:
            fconds[name] = "(" + " AND ".join(parts) + ")"; applied.append(name)

    rng("gvm_score", gvm_min, gvm_max, "gvm")
    rng("g_score", g_min, g_max, "g")
    rng("v_score", v_min, v_max, "v")
    rng("m_score", m_min, m_max, "m")
    if verdict:
        fconds["verdict"] = "(UPPER(verdict) = ANY(%(verdict)s))"
        params["verdict"] = [v.upper() for v in verdict]
        applied.append("verdict")
    rng("seg_rank", seg_rank_min, seg_rank_max, "seg_rank")
    # cc#2145: CAT_2 Sector & Segment -- five locked rows, all columns of the same scored CTE.
    rng("sector_gvm", sector_gvm_min, sector_gvm_max, "sector_gvm")
    rng("sector_rank", sector_rank_min, sector_rank_max, "sector_rank")
    rng("gvm_minus_sector", sector_gap_min, sector_gap_max, "sector_gap")
    rng("sector_member_count", sector_members_min, sector_members_max, "sector_members")
    seg_list = [x.strip() for x in (segments or []) if x and x.strip()]
    if seg_list:
        fconds["segments"] = "(segment = ANY(%(segments)s))"
        params["segments"] = seg_list
        applied.append("segments")
    # cc#2146: CAT_4 Change & Delta. The duration rows pick their column by the selected window;
    # snapshot windows are bound as parameters the CTE always needs (defaults 5 / 5).
    gvm_change_days = gvm_change_days if gvm_change_days in (30, 90, 180) else 30
    m_change_days = m_change_days if m_change_days in (30, 90) else 30
    rng(f"gvm_change_{gvm_change_days}d", gvm_change_min, gvm_change_max, "gvm_change")
    rng(f"m_change_{m_change_days}d", m_change_min, m_change_max, "m_change")
    rng("g_change_90d", g_change_min, g_change_max, "g_change")
    rng("v_change_90d", v_change_min, v_change_max, "v_change")
    rng("sector_gvm_change_90d", sector_change_min, sector_change_max, "sector_change")
    mig = [x.strip().lower() for x in (verdict_migration or []) if x and x.strip().lower() in ("upgraded", "unchanged", "downgraded")]
    if mig:
        fconds["verdict_migration"] = "(verdict_migration = ANY(%(verdict_migration)s))"
        params["verdict_migration"] = mig
        applied.append("verdict_migration")
    if gvm_consistency_k is not None:
        fconds["gvm_consistency"] = "(gvm_pos_snapshots >= %(cons_k)s)"
        params["cons_k"] = max(0, int(gvm_consistency_k))
        applied.append("gvm_consistency")
    params["migr_n"] = max(1, min(int(verdict_migration_n), 400))
    params["cons_n"] = max(1, min(int(gvm_consistency_n), 400))
    params["asof_grace"] = ASOF_GRACE_DAYS

    op_map = _parse_ops(ops)
    ordered = [k for k in _FILTER_ORDER if k in fconds]
    expr, expr_text = _combine(ordered, fconds, op_map)
    where = (" AND " + expr) if expr else ""
    sql = _CAT1_SQL.format(pool_sql=pool_sql, where=where)
    params["limit"] = limit

    # cc#2123: two separate, honest counts -- pool size (no filters) and pass count (filters
    # applied) -- the card's own "two numbers, not one" requirement ("208 F&O -> 31 pass").
    # pool_count is the SCORED pool size (same query /pools uses), never the raw pool table count
    # -- they can legitimately differ (e.g. F&O's 3 index futures carry no GVM score at all), and
    # using the raw count here would make a zero-filter preview look like it silently dropped
    # members that were never scoreable to begin with.
    # cc#2145: every count (total, alone, step) reads the SAME scored CTE the rows come from.
    scored_count_sql = _SCORED_CTE.format(pool_sql=pool_sql) + "SELECT COUNT(*) FROM scored WHERE 1=1"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM (" + pool_sql + ") p WHERE EXISTS "
            "(SELECT 1 FROM gvm_scores g WHERE g.symbol = p.symbol "
            "AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores))")
        pool_count = cur.fetchone()[0]
        cur.execute(scored_count_sql + where, params)
        pass_count = cur.fetchone()[0]
        # cc#2135: each applied filter ALONE against the pool -- real queries, one per applied
        # filter, the same CTE as the combined count, never client-side arithmetic.
        per_filter = {}
        for k in ordered:
            cur.execute(scored_count_sql + " AND " + fconds[k], params)
            per_filter[k] = cur.fetchone()[0]
        # cc#2144: the FUNNEL -- for the k-th applied row, how many rows pass the expression built
        # so far, ((f1 OP2 f2) ... OPk fk): the same _combine over the same prefix of the same
        # page order, run against the same CTE as `count`. Real queries, one per applied row,
        # never client arithmetic. The last step is the full expression, so it equals `count` by
        # construction; under AND each step is <= the one before, under OR it can rise -- reported
        # exactly as the query returns it, never clamped or reordered.
        step_counts = {}
        for i in range(1, len(ordered) + 1):
            step_expr, _ = _combine(ordered[:i], fconds, op_map)
            cur.execute(scored_count_sql + " AND " + step_expr, params)
            step_counts[ordered[i - 1]] = cur.fetchone()[0]

        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        as_of = cur.fetchone()[0]
        # cc#2145 item 5 (FOUNDER_AMENDMENT_16SEP_RANK_IS_SEGMENT_ONLY): segment-size context for the
        # Rank-within-Segment row -- the POOL's segments and their member counts, min / median / max.
        # Shown beside the rank filter, never auto-applied as a filter.
        cur.execute(_SCORED_CTE.format(pool_sql=pool_sql)
                    + "SELECT COUNT(*), MIN(n), percentile_cont(0.5) WITHIN GROUP (ORDER BY n), MAX(n) "
                      "FROM (SELECT segment, COUNT(*) AS n FROM scored GROUP BY segment) z")
        _ss = cur.fetchone()
        segment_stats = {"segments": int(_ss[0] or 0), "min": _ss[1], "median": float(_ss[2]) if _ss[2] is not None else None, "max": _ss[3]}

    # cc#2123: which single filter cut the most -- the smallest of the per-filter counts, so the
    # founder sees the binding gate immediately rather than guessing from the combined result.
    binding = None
    if per_filter and pool_count:
        k, n = min(per_filter.items(), key=lambda kv: kv[1])
        binding = {"filter": k, "cut_to": n, "cut_count": pool_count - n}

    return {
        # cc#2143: `pools` is the selection as sent; `pool`/`pool_label` keep their old names for
        # any reader that still expects one value (joined, so a two-pool selection reads
        # "Large Cap + Nifty 500 (top-500 mcap)"), never silently the first pool alone.
        "pools": keys, "pool_labels": [_POOL_LABEL[k] for k in keys],
        "pool": ",".join(keys), "pool_label": " + ".join(_POOL_LABEL[k] for k in keys),
        "pool_count": pool_count,
        "count": pass_count, "filters_applied": applied, "filter_order": ordered,
        "ops": {k: op_map.get(k, "AND") for k in ordered}, "expression": expr_text,
        "per_filter_counts": per_filter, "step_counts": step_counts, "binding_filter": binding,
        "as_of_date": str(as_of) if as_of else None, "rows": rows,
        "segment_stats": segment_stats,   # cc#2145 item 5
        "delta_windows": {"gvm_change_days": gvm_change_days, "m_change_days": m_change_days,
                          "verdict_migration_n": params["migr_n"], "gvm_consistency_n": params["cons_n"],
                          "asof_grace_days": ASOF_GRACE_DAYS},   # cc#2146
    }


# cc#2135: the page's fixed row order -- "left-to-right" in the combine below means top-to-bottom
# on the page. Kept in ONE place so the page and the endpoint cannot disagree about it.
_FILTER_ORDER = ("gvm", "g", "v", "m", "verdict", "seg_rank",
                 "sector_gvm", "sector_rank", "sector_gap", "sector_members", "segments",   # cc#2145: CAT_2 after CAT_1
                 "gvm_change", "m_change", "g_change", "v_change", "sector_change",
                 "verdict_migration", "gvm_consistency")   # cc#2146: CAT_4 after CAT_2


def _parse_ops(ops: Optional[str]) -> dict:
    """'gvm:AND,verdict:OR' -> {'gvm': 'AND', 'verdict': 'OR'}. Anything unrecognised is AND (the
    spec's stated default)."""
    out = {}
    for part in (ops or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = "OR" if v.strip().upper() == "OR" else "AND"
    return out


def _combine(ordered, fconds, op_map):
    """Left-to-right in page order with explicit parentheses at every step: ((f1 OP2 f2) OP3 f3).
    The first row's own flag has nothing to its left, so it is ignored. Pure -- no DB.
    Returns (sql_fragment, readable_text)."""
    expr, text = "", ""
    for i, k in enumerate(ordered):
        if i == 0:
            expr, text = fconds[k], k
            continue
        op = op_map.get(k, "AND")
        expr = f"({expr} {op} {fconds[k]})"
        text = f"({text} {op} {k})"
    return expr, text


@router.get("/api/qb/universe2/coverage")
def qb_universe2_coverage():
    """cc#2146 item 3: the delta coverage caveat, MEASURED on every call -- for each offset, how many
    of today's scored symbols have an as-of row within the grace window, the as-of date span, the
    daily-history floor and the snapshot count in the last 90 days. The page renders these on the
    rows ("resolves for N of M symbols") and disables an offset that resolves for nobody, with the
    reason, instead of ever returning an empty set silently."""
    sql = ("WITH latest AS (SELECT MAX(score_date) AS d FROM gvm_scores), g AS (SELECT symbol FROM gvm_scores, latest WHERE score_date = latest.d) "
           "SELECT COUNT(*), COUNT(h30.score_date), COUNT(h90.score_date), COUNT(h180.score_date), "
           "MIN(h30.score_date), MAX(h30.score_date), MIN(h90.score_date), MAX(h90.score_date), MIN(h180.score_date), MAX(h180.score_date), "
           "(SELECT d FROM latest) FROM g\n"
           + _asof_lateral("h30", 30, "h.score_date") + _asof_lateral("h90", 90, "h.score_date") + _asof_lateral("h180", 180, "h.score_date"))
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, {"asof_grace": ASOF_GRACE_DAYS})
        r = cur.fetchone()
        cur.execute("SELECT MIN(score_date) FROM (SELECT score_date FROM gvm_history WHERE score_date >= DATE '2026-01-01' "
                    "GROUP BY 1 HAVING COUNT(*) >= 500) f")
        floor = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT score_date) FROM gvm_history, (SELECT MAX(score_date) AS d FROM gvm_scores) l "
                    "WHERE score_date > l.d - 90 AND score_date <= l.d")
        snaps = cur.fetchone()[0]
    latest_d = r[10]
    era_days = (latest_d - floor).days if (latest_d and floor) else 0
    def win(n, c, lo, hi):
        # ENABLED only when the daily era is at least n days deep (item 3: "disabled until history
        # reaches it"). A handful of pre-era snapshots can still resolve inside the grace window
        # (12 symbols at 180d today, from a 2026-03-01 snapshot) -- counted, but not an offer.
        enabled = era_days >= n
        reason = None if enabled else (f"daily history starts {floor} ({era_days} days back); {n}-day changes "
                                       f"resolve from {(floor + __import__('datetime').timedelta(days=n)).isoformat()}")
        return {"days": n, "resolves": int(c or 0), "of": int(r[0] or 0), "asof_from": str(lo) if lo else None,
                "asof_to": str(hi) if hi else None, "enabled": enabled, "reason": reason}
    return {"as_of_date": str(r[10]) if r[10] else None, "symbols": int(r[0] or 0),
            "windows": {"30": win(30, r[1], r[4], r[5]), "90": win(90, r[2], r[6], r[7]), "180": win(180, r[3], r[8], r[9])},
            "daily_history_floor": str(floor) if floor else None, "daily_era_days": era_days, "snapshots_last_90d": int(snaps or 0),
            "asof_grace_days": ASOF_GRACE_DAYS}


@router.get("/api/qb/universe2/segments")
def qb_universe2_segments():
    """cc#2145: every segment of the scored universe at the latest score_date with its member count,
    mcap-weighted sector GVM (3 dp, the /sector number) and dense rank -- the list the Segments
    multi-select and the sector rows are read against. Universe-wide by design (not pool-scoped),
    same CTE the preview joins on, so the two can never disagree."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("WITH " + _UNI_SECT_SQL + " SELECT segment, sector_member_count, sector_gvm, sector_rank "
                    "FROM sect_ranked ORDER BY segment")
        segs = [{"segment": r[0], "members": int(r[1]), "sector_gvm": float(r[2]) if r[2] is not None else None,
                 "sector_rank": int(r[3])} for r in cur.fetchall()]
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        as_of = cur.fetchone()[0]
    return {"segments": segs, "count": len(segs), "as_of_date": str(as_of) if as_of else None}


@router.get("/qb/universe2", response_class=HTMLResponse)
def qb_universe2_page():
    """cc#2123: the new Universe builder page. Served with its own reader (same reasoning
    trade_wall_endpoints.web_trades() states: reaching a repo-root file through mobile_endpoints._page
    would need "../", a path-traversal shape not worth introducing for a constant filename)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorr_qb_universe.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return HTMLResponse("Universe builder is not wired yet.", status_code=404,
                             headers={"Cache-Control": "no-store"})
