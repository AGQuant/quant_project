"""
ops_peer_benchmark.py — cc#593 OPS-METRICS PEER-BENCHMARK COMPUTE (single-level).
================================================================================
Founder-locked build (session_log 7117 spec + 7118 KPI defs + 7132 peer-set/window LOCK).
CC owns the compute per the 21-Jul founder decision ("CC builds compute only"; the review PDF /
cc#601 stays Claude-web). Display + screening ONLY — NEVER a GVM-score input.

MODEL (single level, 4Q trend dropped 21-Jul): for each stock × core metric, the LATEST-available
quarter value + a peer benchmark (avg / median / percentile) computed within the stock's peer
segment over a contemporaneous window.

LOCKED RULES (session_log 7132 OPS_METRICS_PEERSET_AND_WINDOW_LOCK_V1):
  * Peer set = reconciled canonical sector (22-registry); collapse sector variants first.
  * BFSI split via screener_raw.Industry — BANKS (Private/Public/Other Bank) and NBFC (NBFC + HFC +
    Financial Institution + Microfinance) are SEPARATE segments everywhere; each metric benchmarked
    within its OWN segment. CASA / deposit_growth / cost_to_income are BANKS-only.
  * Quarter window: per (segment, metric) the peer pool = only symbols whose latest value quarter is
    within the LAST 2 REPORTED quarters (QxFYxx ordering); stale >2Q-old dropped so comparison stays
    contemporaneous. Per (symbol, metric) use the latest-available quarter value.
  * percent_rank over the in-window pool; direction-adjusted per registry.direction so HIGH pctile
    ALWAYS = good on the card (lower_better metrics invert). better/worse vs median.
  * min_peers = 2 (founder 21-Jul, lowered from 5); below 2 => value shown, percentile NULL, thin_peer.

RATIO NORMALIZATION (session_log 7118 ratio_normalization_v2): peer-benchmark must be size-neutral.
Already-ratio / per-unit / growth / margin metrics benchmark as-is. Pure absolutes (order_book, aum,
bookings, volumes, headcount, store adds, TCV, beds ...) are NOT comparable across different-sized
peers — they DISPLAY their value but carry NO percentile here (reason='absolute_needs_normalization');
their size-neutral ratios (order_book_to_sales / book_to_bill / *_growth) are a cc#594 follow-up that
extracts the raw numerators + derives the ratio against screener_raw."Sales". Benchmarking an absolute
across peers would be dishonest, so we never do it.

Output: materialized table ops_peer_benchmark (one row per symbol × metric), read by the GVM card
ops-block + the sector-aware screener. Idempotent full rebuild — small data (~1370 rows / 506 syms),
so a truncate-and-rebuild is cheap and keeps the pool math trivially correct.
"""
import json
import logging
import re
import statistics
import time
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query

log = logging.getLogger("scorr.ops_peer_benchmark")
router = APIRouter(prefix="/api/ops-peer", tags=["ops_peer_benchmark"])

# ── metric_name canon: lower() first, then these cross-name (non-case) synonym overrides ──────────
_CANON_OVERRIDE = {
    "provision_coverage": "pcr",
    "credit_growth_yoy": "credit_growth",
    "ebitda_margin": "ebitda_margin_pct",
    "realisation_per_mt": "realization_per_t",
    "volume_mt": "volumes_mn_t",
    "volume_mn_t": "volumes_mn_t",
    "ebitda_per_mt": "ebitda_per_t",
    "capacity_utilisation_pct": "capacity_util_pct",
    "data_usage_per_user_gb": "data_per_sub_gb",
    "arpob_day": "arpob",
    "deal_wins_tcv_cr": "tcv_deal_wins",
    "deal_wins_tcv_mn": "tcv_deal_wins",
}


def _canon_metric(name: str) -> str:
    k = (name or "").strip().lower()
    return _CANON_OVERRIDE.get(k, k)


# ── sector string reconcile -> 22-registry canonical (session_log 7132 collapse list) ─────────────
_SECTOR_RECONCILE = {
    "it_tech": "IT", "it": "IT",
    "real_estate": "Realty", "realty": "Realty",
    "auto_components": "Auto", "auto": "Auto",
    "metals_steel": "Metals_Steel", "steel_tubes": "Metals_Steel",
    "power_utilities": "Power_Energy", "power_trading": "Power_Energy", "power_energy": "Power_Energy",
    "city_gas": "Oil_Gas", "oil_gas": "Oil_Gas",
    "capital_markets": "Financial_Services_Markets", "exchanges_ratings": "Financial_Services_Markets",
}


def _reconcile_sector(sector: str) -> str:
    s = (sector or "").strip()
    return _SECTOR_RECONCILE.get(s.lower(), s)


# ── BFSI split via screener_raw.Industry (session_log 7132) ───────────────────────────────────────
_BANK_INDUSTRIES = {"Private Sector Bank", "Public Sector Bank", "Other Bank"}
_NBFC_INDUSTRIES = {"Non Banking Financial Company (NBFC)", "Housing Finance Company",
                    "Financial Institution", "Microfinance Institutions"}
# canonical (post-reconcile) sector strings that denote the combined BFSI registry to be split
_BFSI_SECTORS = {"banking_nbfc", "banking", "nbfc", "finance", "msme_finance", "banks"}
# metrics that live in the Banking_NBFC registry (used to route a BFSI stock's row into its segment)
_BFSI_METRICS = {"nim", "gnpa_pct", "nnpa_pct", "casa_pct", "roa", "credit_growth",
                 "deposit_growth", "credit_cost", "cost_to_income", "pcr"}
_BANKS_ONLY_METRICS = {"casa_pct", "deposit_growth", "cost_to_income"}   # NBFC has none

# ── ratio-normalization (7118): absolutes DISPLAY but carry no percentile (size-not-neutral) ──────
_ABSOLUTE_METRICS = {
    "volumes_mn_t", "volumes_units_by_segment", "volume_teu_mt", "aum_cr", "bookings_value_cr",
    "bookings_units", "store_adds", "headcount_net_adds", "tcv_deal_wins", "bed_count",
    "bed_count_adds", "sip_inflows_cr", "throughput_mmt", "collections_cr", "test_volume_mn",
    "rooms_pipeline", "subscribers_net_adds", "car",
    # cc#594 enriched absolutes — display value, no percentile (7118: benchmark the derived ratio,
    # e.g. order_book_to_sales / book_to_bill, not the size-non-neutral absolute).
    "order_book", "order_inflow", "cargo_volume_mt", "throughput_teu",
}

_DEFAULT_DIRECTION = "higher_better"

# Authoritative lower_better override — some lower_better metrics (cost_to_income, combined_ratio,
# claims_ratio, loss_ratio) are absent from sector_kpi_registry, so a registry-only lookup would
# default them to higher_better and INVERT their percentile. This set wins over the registry.
_LOWER_BETTER = {
    "gnpa_pct", "nnpa_pct", "credit_cost", "cost_to_income", "attrition_pct", "net_debt_ebitda",
    "power_fuel_cost_per_t", "churn_pct", "alos_days", "claims_ratio", "loss_ratio", "combined_ratio",
}


def _direction_for(metric: str, registry_direction: Dict[str, str]) -> str:
    if metric in _LOWER_BETTER:
        return "lower_better"
    return registry_direction.get(metric, _DEFAULT_DIRECTION)


def _parse_qord(quarter: str) -> Optional[int]:
    """QxFYxx -> sortable ordinal FY*10+Q (Q4FY26 -> 264). Non-QxFYxx labels (e.g. 'FY25', 'TTM')
    are UNorderable for the contemporaneous window and excluded from the pool."""
    q = (quarter or "").strip().upper()
    m = re.match(r"Q([1-4])FY(\d{2})$", q)
    if not m:
        return None
    return int(m.group(2)) * 10 + int(m.group(1))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ops_peer_benchmark (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    segment TEXT NOT NULL,
    metric TEXT NOT NULL,
    metric_display TEXT,
    direction TEXT,
    tier TEXT,
    value NUMERIC,
    unit TEXT,
    as_of_quarter TEXT,
    confidence TEXT,
    peer_n INTEGER,
    peer_avg NUMERIC,
    peer_median NUMERIC,
    pctile_raw NUMERIC,
    pctile_good NUMERIC,
    vs_peer_delta NUMERIC,
    better_than_median BOOLEAN,
    thin_peer BOOLEAN,
    no_percentile_reason TEXT,
    computed_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, segment, metric)
);
CREATE INDEX IF NOT EXISTS idx_opb_symbol ON ops_peer_benchmark(symbol);
CREATE INDEX IF NOT EXISTS idx_opb_segment_metric ON ops_peer_benchmark(segment, metric);
"""

MIN_PEERS = 2        # session_log 7132 v1.1 (founder 21-Jul, lowered from 5)
WINDOW_QUARTERS = 2  # last 2 reported quarters per (segment, metric)


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def _load_registry(cur):
    """Returns (direction_by_metric, display_by_(metric,sector), tier_by_(metric,sector)). Direction
    is consistent per metric across the registry, so a metric->direction map is safe."""
    cur.execute("SELECT sector, metric_name, display_name, direction, tier FROM sector_kpi_registry")
    direction: Dict[str, str] = {}
    display: Dict[Tuple[str, str], str] = {}
    tier: Dict[Tuple[str, str], str] = {}
    for sec, metric, disp, dirn, tr in cur.fetchall():
        m = _canon_metric(metric)
        direction.setdefault(m, dirn or _DEFAULT_DIRECTION)
        display[(m, sec)] = disp or metric
        tier[(m, sec)] = tr or "core"
    return direction, display, tier


def _industry_map(cur) -> Dict[str, str]:
    """symbol -> BANKS | NBFC (via screener_raw.Industry). Symbols in neither bucket are absent."""
    cur.execute("""SELECT UPPER(nse_code), "Industry" FROM screener_raw
                   WHERE nse_code IS NOT NULL AND nse_code<>''""")
    out: Dict[str, str] = {}
    for sym, industry in cur.fetchall():
        if industry in _BANK_INDUSTRIES:
            out[sym] = "BANKS"
        elif industry in _NBFC_INDUSTRIES:
            out[sym] = "NBFC"
    return out


def _has_casa(cur) -> set:
    """Symbols carrying any casa row — the BFSI fallback (has casa => BANKS) per 7132."""
    cur.execute("SELECT DISTINCT symbol FROM sector_ops_metrics WHERE lower(metric_name)='casa_pct'")
    return {r[0] for r in cur.fetchall()}


def _segment_for(symbol: str, metric: str, recon_sector: str,
                 industry: Dict[str, str], has_casa: set) -> Optional[str]:
    """Route a (symbol, metric) into its peer segment. BFSI (a BFSI sector OR a BFSI-only metric)
    splits BANKS vs NBFC via Industry, with the 7132 fallback (no Industry match -> BANKS if it has a
    casa row else NBFC). Bank-only metrics on a NBFC are dropped (returns None)."""
    is_bfsi = (recon_sector.lower() in _BFSI_SECTORS) or (metric in _BFSI_METRICS)
    if is_bfsi:
        seg = industry.get(symbol)
        if seg is None:
            seg = "BANKS" if symbol in has_casa else "NBFC"
        if seg == "NBFC" and metric in _BANKS_ONLY_METRICS:
            return None
        return seg
    return recon_sector or None


def rebuild(conn) -> dict:
    """Full idempotent rebuild of ops_peer_benchmark from sector_ops_metrics. Small data -> truncate
    + recompute. Returns a summary and writes an ops_log audit."""
    ensure_table(conn)
    t0 = time.time()
    with conn.cursor() as cur:
        direction, display, tier = _load_registry(cur)
        industry = _industry_map(cur)
        has_casa = _has_casa(cur)
        cur.execute("""SELECT symbol, sector, quarter, metric_name, metric_value, unit, confidence
                       FROM sector_ops_metrics WHERE metric_value IS NOT NULL""")
        raw = cur.fetchall()

    # 1. normalize + latest-per (symbol, segment, metric), QxFYxx quarters only
    latest: Dict[Tuple[str, str, str], Tuple[int, str, float, str, str]] = {}
    for sym, sector, quarter, mname, mval, unit, conf in raw:
        qord = _parse_qord(quarter)
        if qord is None:
            continue
        metric = _canon_metric(mname)
        recon = _reconcile_sector(sector)
        seg = _segment_for(sym, metric, recon, industry, has_casa)
        if not seg:
            continue
        key = (sym, seg, metric)
        prev = latest.get(key)
        if prev is None or qord > prev[0]:
            latest[key] = (qord, quarter, float(mval), unit, conf)

    # 2. group into (segment, metric) pools, apply the last-2-reported-quarters window
    pools: Dict[Tuple[str, str], List[Tuple[str, int, str, float, str, str]]] = {}
    for (sym, seg, metric), (qord, quarter, val, unit, conf) in latest.items():
        pools.setdefault((seg, metric), []).append((sym, qord, quarter, val, unit, conf))

    rows_out = []
    seg_set = set()
    thin = 0
    absolutes = 0
    for (seg, metric), members in pools.items():
        seg_set.add(seg)
        dirn = _direction_for(metric, direction)
        is_absolute = metric in _ABSOLUTE_METRICS
        distinct_q = sorted({m[1] for m in members}, reverse=True)[:WINDOW_QUARTERS]
        window_cut = distinct_q[-1] if distinct_q else None
        in_window = [m for m in members if window_cut is not None and m[1] >= window_cut]
        pool_vals = [m[3] for m in in_window]
        peer_n = len(pool_vals)
        # Peer stats only for benchmarkable (non-absolute) metrics — a cross-peer average of an
        # absolute (AUM, volumes, headcount ...) is size-non-neutral and misleading (7118 doctrine).
        if is_absolute:
            peer_avg = peer_median = None
        else:
            peer_avg = round(statistics.fmean(pool_vals), 4) if pool_vals else None
            peer_median = round(statistics.median(pool_vals), 4) if pool_vals else None
        disp = display.get((metric, seg)) or next(
            (d for (mm, _s), d in display.items() if mm == metric), metric)
        tr = tier.get((metric, seg)) or next(
            (t for (mm, _s), t in tier.items() if mm == metric), "core")
        for sym, qord, quarter, val, unit, conf in members:
            thin_peer = peer_n < MIN_PEERS
            in_win = window_cut is not None and qord >= window_cut
            pctile_raw = pctile_good = vs_delta = None
            better = None
            reason = None
            if is_absolute:
                reason = "absolute_needs_normalization"   # 7118 doctrine — cc#594 derives the ratio
            elif thin_peer or not in_win:
                reason = "thin_peer" if thin_peer else "stale_quarter"
            else:
                # percent_rank ascending over the in-window pool: (count strictly-less)/(n-1).
                # Direction-adjust so HIGH pctile = good on the card.
                less = sum(1 for v in pool_vals if v < val)
                pctile_raw = round(less / (peer_n - 1), 4) if peer_n > 1 else None
                pctile_good = pctile_raw
                if dirn == "lower_better" and pctile_raw is not None:
                    pctile_good = round(1.0 - pctile_raw, 4)
                vs_delta = round(val - peer_avg, 4) if peer_avg is not None else None
                if peer_median is not None:
                    better = (val >= peer_median) if dirn != "lower_better" else (val <= peer_median)
            rows_out.append((sym, seg, metric, disp, dirn, tr, val, unit, quarter, conf,
                             peer_n, peer_avg, peer_median, pctile_raw, pctile_good, vs_delta,
                             better, thin_peer, reason))
            if is_absolute:
                absolutes += 1
            elif thin_peer:
                thin += 1

    with conn.cursor() as cur:
        cur.execute("TRUNCATE ops_peer_benchmark RESTART IDENTITY")
        cur.executemany("""
            INSERT INTO ops_peer_benchmark
              (symbol, segment, metric, metric_display, direction, tier, value, unit, as_of_quarter,
               confidence, peer_n, peer_avg, peer_median, pctile_raw, pctile_good, vs_peer_delta,
               better_than_median, thin_peer, no_percentile_reason, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        """, rows_out)
        conn.commit()

    summary = {"rows": len(rows_out), "segments": len(seg_set), "pools": len(pools),
               "thin_peer_cells": thin, "absolute_display_only": absolutes,
               "symbols": len({r[0] for r in rows_out}),
               "elapsed_s": round(time.time() - t0, 2)}
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                        ("ops_peer_benchmark", "cc#593 peer-benchmark rebuild",
                         json.dumps(summary, default=str)))
        conn.commit()
    except Exception as e:
        log.warning(f"ops_log write failed: {e}")
    log.info(f"ops_peer_benchmark rebuild: {summary}")
    return summary


def card_block(conn, symbol: str) -> List[dict]:
    """Per-symbol ops-block for the GVM card: core metrics first, each with value + as-of quarter +
    peer badge (direction-adjusted percentile, vs-peer, better/worse, thin-peer flag)."""
    symbol = (symbol or "").strip().upper()
    with conn.cursor() as cur:
        cur.execute("""SELECT segment, metric, metric_display, direction, tier, value, unit,
                              as_of_quarter, confidence, peer_n, peer_avg, peer_median,
                              pctile_raw, pctile_good, vs_peer_delta, better_than_median,
                              thin_peer, no_percentile_reason
                       FROM ops_peer_benchmark WHERE symbol=%s
                       ORDER BY (tier='core') DESC, metric""", (symbol,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def run_rebuild() -> dict:
    """Open a DB connection and rebuild the materialization. Entry point for the scheduler job,
    the manual admin endpoint, and the app-startup one-shot."""
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        return rebuild(conn)
    finally:
        conn.close()


def screen(conn, segment: str, metric: str, op: str, limit: int = 100) -> List[dict]:
    """Sector-aware screener over the direction-adjusted percentile. op in:
    'top_quartile' (pctile_good>=0.75), 'bottom_quartile' (<=0.25),
    'above_peer_avg' / 'below_peer_avg' (better_than_median, direction-aware)."""
    metric = _canon_metric(metric)
    where = ["segment=%s", "metric=%s"]
    params: List = [segment, metric]
    if op == "top_quartile":
        where.append("pctile_good >= 0.75")
    elif op == "bottom_quartile":
        where.append("pctile_good <= 0.25 AND pctile_good IS NOT NULL")
    elif op == "above_peer_avg":
        where.append("better_than_median = TRUE")
    elif op == "below_peer_avg":
        where.append("better_than_median = FALSE")
    sql = ("SELECT symbol, value, unit, as_of_quarter, pctile_good, vs_peer_delta, peer_n "
           "FROM ops_peer_benchmark WHERE " + " AND ".join(where) +
           " ORDER BY pctile_good DESC NULLS LAST LIMIT %s")
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── endpoints ─────────────────────────────────────────────────────────────────────────────────────
import os
_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _check_admin(tok: Optional[str]):
    if not _ADMIN_TOKEN or tok != _ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="admin token required")


@router.post("/rebuild")
def rebuild_now(x_admin_token: Optional[str] = Header(None)):
    """cc#593: force a peer-benchmark materialization rebuild (also runs nightly + after each T+1)."""
    _check_admin(x_admin_token)
    return run_rebuild()


@router.get("/card/{symbol}")
def card(symbol: str):
    """Per-symbol ops-block (value + as-of quarter + peer badge) for the GVM card."""
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        return {"symbol": symbol.strip().upper(), "metrics": card_block(conn, symbol)}
    finally:
        conn.close()


@router.get("/screen")
def screen_endpoint(segment: str = Query(...), metric: str = Query(...),
                    op: str = Query("top_quartile"), limit: int = Query(100)):
    """Sector-aware screener: top/bottom quartile or above/below peer-avg on a segment×metric."""
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        return {"segment": segment, "metric": _canon_metric(metric), "op": op,
                "results": screen(conn, segment, metric, op, limit)}
    finally:
        conn.close()


@router.on_event("startup")
async def _startup_materialize():
    """cc#593: one-shot materialization on boot IF the table is missing/empty, so the compute output
    exists immediately after deploy (Claude-web's cc#601 PDF + the GVM card read it). Steady-state
    refresh is the nightly scheduler job + the cc#596 post-T+1 chain — this only seeds an empty table.
    Runs in a daemon thread so it never blocks app boot."""
    import threading

    def _seed():
        try:
            import fyers_feed
            conn = fyers_feed.get_db()
            try:
                ensure_table(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM ops_peer_benchmark")
                    n = cur.fetchone()[0]
                if n == 0:
                    log.info("cc#593: ops_peer_benchmark empty on boot — seeding materialization")
                    rebuild(conn)
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"cc#593 startup materialize skipped: {e}")

    threading.Thread(target=_seed, name="cc593-opb-seed", daemon=True).start()


@router.get("/segments")
def segments():
    """List materialized segments + per-segment metric coverage (for the screener UI + PDF index)."""
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT segment, COUNT(DISTINCT symbol) AS syms,
                                  COUNT(DISTINCT metric) AS metrics,
                                  COUNT(*) FILTER (WHERE pctile_good IS NOT NULL) AS benchmarked
                           FROM ops_peer_benchmark GROUP BY segment ORDER BY segment""")
            cols = [c[0] for c in cur.description]
            return {"segments": [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        conn.close()

