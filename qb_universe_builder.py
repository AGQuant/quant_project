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

Pool 3 (Nifty 500) carries a real, measured data-quality caveat: nifty500_universe
has exactly ONE built_at (2026-06-02) and has not moved since -- checked directly,
this session, not assumed from the spec. Surfaced on the page as a stale badge, per
the card's own instruction that a pool must never silently claim to be current.
"""
import os
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
# source table (input_raw.nse_code, futures_universe.symbol, nifty500_universe.symbol) -- the SQL
# below aliases every one to `symbol` so the caller never needs to know which table it came from.
_CAP_BANDS = {
    "cap_large": ("Large Cap", "large"),
    "cap_mid": ("Mid Cap", "mid"),
    "cap_small": ("Small Cap", "small"),
    "cap_micro": ("Micro Cap", "micro"),
}

_POOL_SQL = {
    "cap_large": "SELECT nse_code AS symbol FROM input_raw WHERE cap_category = 'large'",
    "cap_mid": "SELECT nse_code AS symbol FROM input_raw WHERE cap_category = 'mid'",
    "cap_small": "SELECT nse_code AS symbol FROM input_raw WHERE cap_category = 'small'",
    "cap_micro": "SELECT nse_code AS symbol FROM input_raw WHERE cap_category = 'micro'",
    "fo": "SELECT symbol FROM futures_universe WHERE is_active = true",
    "nifty500": "SELECT symbol FROM nifty500_universe",
}
_POOL_LABEL = {
    "cap_large": "Large Cap", "cap_mid": "Mid Cap", "cap_small": "Small Cap", "cap_micro": "Micro Cap",
    "fo": "All F&O Stocks", "nifty500": "Nifty 500 (top-500 mcap)",
}


_POOL_GROUP = {"cap_large": "cap_band", "cap_mid": "cap_band", "cap_small": "cap_band",
               "cap_micro": "cap_band", "fo": "fo", "nifty500": "nifty500"}


@router.get("/api/qb/universe2/pools")
def qb_universe2_pools():
    """Every pool's live count. Cap bands read input_raw.cap_category (session_log 85's own locked
    band edges: large=mcap_rank 1-100 etc, already computed there -- this endpoint reads the
    classification, it does not re-derive it). F&O reads the registry (is_active), same convention
    every other surface in this codebase uses for the active futures list. Nifty 500 is OUR OWN
    top-500-by-market-cap build (founder ruling, this card: "That is our own calculation"), not
    real NSE index membership -- labelled as such.

    cc#2123: every pool's RAW size can differ from its SCORED size (how many of its members carry
    a current gvm_scores row) -- measured live this session, not assumed: F&O 208 raw / 205 scored
    (3 are index futures, BANKNIFTY/NIFTY/NIFTY50, GVM does not apply to an index), micro-cap 761/
    732, small-cap 748/744, Nifty 500 500/497. Large and mid have zero gap. The count shown and
    returned is always the SCORED count -- it is the one CAT_1 filtering can actually act on, and
    it is what must agree with /preview's own pool_count with zero filters applied. The raw count
    and the reason for any gap ride along in `note` so nothing is silently dropped unexplained."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(built_at) FROM nifty500_universe")
        n500_count, n500_built = cur.fetchone()
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
        if key == "nifty500":
            # cc#2123: measured this session, not assumed -- nifty500_universe has exactly one
            # built_at and has not been rebuilt since. A rebuild-cadence fix is its own follow-up
            # card (data pipeline, not this page); until it lands, the page must say so plainly.
            entry["stale"] = True
            entry["built_at"] = str(n500_built) if n500_built else None
            built_date = str(n500_built)[:10] if n500_built else "ranking date unknown"
            entry["stale_note"] = (f"ranked as of {built_date} -- this pool has not been rebuilt "
                                    "since; market-cap moves daily but this ranking has not moved in months")
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
    pool: str,
    gvm_min: Optional[float] = None, gvm_max: Optional[float] = None,
    g_min: Optional[float] = None, g_max: Optional[float] = None,
    v_min: Optional[float] = None, v_max: Optional[float] = None,
    m_min: Optional[float] = None, m_max: Optional[float] = None,
    verdict: Optional[List[str]] = Query(None),
    seg_rank_min: Optional[int] = None, seg_rank_max: Optional[int] = None,
    limit: int = 500,
):
    """cc#2123: pool -> optional CAT_1 filters -> count + ranked rows. Every value read straight
    off gvm_scores' own current snapshot (score_date = MAX) -- no intraday feed anywhere on this
    path, per the founder's separate EOD-basis ruling on this same card (there is no live-price
    join here at all to violate it). filters_applied + binding_filter let the caller show WHY a
    count is what it is, per the card's own click-through requirement."""
    if pool not in _POOL_SQL:
        return {"error": "unknown pool", "known": sorted(_POOL_SQL)}
    limit = max(1, min(int(limit), 2000))

    conds, params, applied = [], {}, []

    def rng(col, lo, hi, name):
        if lo is not None:
            conds.append(f"{col} >= %({name}_lo)s"); params[name + "_lo"] = lo; applied.append(name + "_min")
        if hi is not None:
            conds.append(f"{col} <= %({name}_hi)s"); params[name + "_hi"] = hi; applied.append(name + "_max")

    rng("gvm_score", gvm_min, gvm_max, "gvm")
    rng("g_score", g_min, g_max, "g")
    rng("v_score", v_min, v_max, "v")
    rng("m_score", m_min, m_max, "m")
    rng("seg_rank", seg_rank_min, seg_rank_max, "seg_rank")
    if verdict:
        conds.append("UPPER(verdict) = ANY(%(verdict)s)")
        params["verdict"] = [v.upper() for v in verdict]
        applied.append("verdict")

    where = (" AND " + " AND ".join(conds)) if conds else ""
    sql = _CAT1_SQL.format(pool_sql=_POOL_SQL[pool], where=where)
    params["limit"] = limit

    # cc#2123: two separate, honest counts -- pool size (no filters) and pass count (filters
    # applied) -- the card's own "two numbers, not one" requirement ("208 F&O -> 31 pass").
    # pool_count is the SCORED pool size (same query /pools uses), never the raw pool table count
    # -- they can legitimately differ (e.g. F&O's 3 index futures carry no GVM score at all), and
    # using the raw count here would make a zero-filter preview look like it silently dropped
    # members that were never scoreable to begin with.
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM (" + _POOL_SQL[pool] + ") p WHERE EXISTS "
            "(SELECT 1 FROM gvm_scores g WHERE g.symbol = p.symbol "
            "AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores))")
        pool_count = cur.fetchone()[0]
        cur.execute(
            "WITH pool AS (" + _POOL_SQL[pool] + "), scored AS ("
            "SELECT g.symbol, g.segment, g.gvm_score, g.g_score, g.v_score, g.m_score, "
            "RANK() OVER (PARTITION BY g.segment ORDER BY g.gvm_score DESC) AS seg_rank "
            "FROM gvm_scores g JOIN pool p ON p.symbol = g.symbol "
            "WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)) "
            "SELECT COUNT(*) FROM scored WHERE 1=1" + where, params)
        pass_count = cur.fetchone()[0]

        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        as_of = cur.fetchone()[0]

    # cc#2123: which single filter cut the most -- run each APPLIED filter alone against the
    # pool and report the smallest resulting count, so the founder sees the binding gate
    # immediately rather than having to guess from the combined result.
    binding = None
    if applied and pool_count:
        with _conn() as conn, conn.cursor() as cur:
            best = None
            for name in applied:
                single_where = ""
                single_params = {"limit": limit}
                if name.endswith("_min") or name.endswith("_max"):
                    base = name.rsplit("_", 1)[0]
                    col = {"gvm": "gvm_score", "g": "g_score", "v": "v_score", "m": "m_score",
                           "seg_rank": "seg_rank"}.get(base)
                    if not col:
                        continue
                    key = base + ("_lo" if name.endswith("_min") else "_hi")
                    if key not in params:
                        continue
                    op = ">=" if name.endswith("_min") else "<="
                    single_where = f" AND {col} {op} %({key})s"
                    single_params[key] = params[key]
                elif name == "verdict":
                    single_where = " AND UPPER(verdict) = ANY(%(verdict)s)"
                    single_params["verdict"] = params["verdict"]
                else:
                    continue
                cur.execute(
                    "WITH pool AS (" + _POOL_SQL[pool] + "), scored AS ("
                    "SELECT g.symbol, g.segment, g.gvm_score, g.g_score, g.v_score, g.m_score, g.verdict, "
                    "RANK() OVER (PARTITION BY g.segment ORDER BY g.gvm_score DESC) AS seg_rank "
                    "FROM gvm_scores g JOIN pool p ON p.symbol = g.symbol "
                    "WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)) "
                    "SELECT COUNT(*) FROM scored WHERE 1=1" + single_where, single_params)
                n = cur.fetchone()[0]
                if best is None or n < best[1]:
                    best = (name, n)
            if best:
                binding = {"filter": best[0], "cut_to": best[1], "cut_count": pool_count - best[1]}

    return {
        "pool": pool, "pool_label": _POOL_LABEL[pool], "pool_count": pool_count,
        "count": pass_count, "filters_applied": applied, "binding_filter": binding,
        "as_of_date": str(as_of) if as_of else None, "rows": rows,
    }


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
