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
_CAT1_SQL = """
WITH pool AS ({pool_sql}),
scored AS (
    SELECT g.symbol, g.company_name, g.segment, g.gvm_score, g.g_score, g.v_score, g.m_score,
           g.verdict, g.market_cap, g.price,
           RANK() OVER (PARTITION BY g.segment ORDER BY g.gvm_score DESC) AS seg_rank,
           COUNT(*) OVER (PARTITION BY g.segment) AS seg_size,
           ROUND((g.gvm_score - AVG(g.gvm_score) OVER (PARTITION BY g.segment))::numeric, 2) AS gap_vs_sector
      FROM gvm_scores g
      JOIN pool p ON p.symbol = g.symbol
     WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
)
SELECT * FROM scored WHERE 1=1{where}
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
    scored_count_sql = (
        "WITH pool AS (" + pool_sql + "), scored AS ("
        "SELECT g.symbol, g.segment, g.gvm_score, g.g_score, g.v_score, g.m_score, g.verdict, "
        "RANK() OVER (PARTITION BY g.segment ORDER BY g.gvm_score DESC) AS seg_rank "
        "FROM gvm_scores g JOIN pool p ON p.symbol = g.symbol "
        "WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)) "
        "SELECT COUNT(*) FROM scored WHERE 1=1")
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
    }


# cc#2135: the page's fixed row order -- "left-to-right" in the combine below means top-to-bottom
# on the page. Kept in ONE place so the page and the endpoint cannot disagree about it.
_FILTER_ORDER = ("gvm", "g", "v", "m", "verdict", "seg_rank")


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
