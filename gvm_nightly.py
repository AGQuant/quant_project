"""
GVM Nightly Recompute - Scorr
===============================
Self-contained FastAPI router. Server-side GVM recompute driven entirely by
the LIVE DB tables (no CSV on disk).

GVM = G + V + M, where:
  G (Growth)  + V (Value)  -> from screener_raw  (weekly upload; fundamentals)
  M (Momentum)             -> from momentum_scores (DAILY; price-driven)

This split is the core of the GVM TREND:
  - Fundamentals (G,V) step-change only when a new screener CSV is uploaded.
  - Momentum (M) recomputes daily from raw_prices via momentum_daily.py.
  - Combined daily snapshot -> gvm_history -> the trend line.

Workflow:
  1. POST /api/admin/load_screener_from_drive  {file_id}  -> clean-replace screener_raw (weekly)
  2. POST /api/gvm/recompute                              -> refresh momentum (daily) + score all:
         - gvm_history  (APPEND one dated row per stock; the trend table)
         - gvm_scores   (REPLACE latest snapshot; canonical read table)
         - sector_ratings (REPLACE; mcap-weighted segment GVM — wired daily)

Verdict framework (Arpit):
  >= 8.0  -> Excellent
  7.0-8.0 -> Good
  6.0-7.0 -> Average
  < 6.0   -> Weak
"""

import os
import logging
from datetime import date
from typing import Optional, Dict, List

import psycopg
from psycopg.rows import dict_row
import pandas as pd
import numpy as np
from fastapi import APIRouter, HTTPException, Request, Header

from gvm_engine import api_g_score, api_v_score
import momentum_daily

log = logging.getLogger("scorr.gvm_nightly")

router = APIRouter(tags=["gvm-nightly"])

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


# ============================================================
# SCREENER COLUMN MAPPING (raw Screener header -> live screener_raw col)
# ============================================================
SCREENER_COLUMNS = {
    "Current Price": "price", "Sales growth 5Years": "sales_growth_5y",
    "Sales growth 3Years": "sales_growth_3y", "Profit growth 5Years": "profit_growth_5y",
    "Profit growth 3Years": "profit_growth_3y", "YOY Quarterly sales growth": "qoq_sales_growth",
    "YOY Quarterly profit growth": "qoq_profit_growth", "OPM": "opm",
    "OPM latest quarter": "opm_latest_q", "OPM preceding year quarter": "opm_prev_year_q",
    "Fixed Asset Growth": "fixed_asset_growth", "FII holding": "fii_holding",
    "DII holding": "dii_holding", "Change in FII holding": "fii_change",
    "Change in DII holding": "dii_change", "Return on capital employed": "roce",
    "Interest Coverage Ratio": "interest_coverage", "Dividend yield": "dividend_yield",
    "Price to Earning": "pe", "Historical PE 10Years": "historical_pe",
    "Industry PE": "segment_pe", "Return over 1year": "return_1y",
    "Return over 3years": "return_3y", "DMA 50": "dma_50", "DMA 200": "dma_200",
    "52w Index": "return_52w_vs_index", "Market Capitalization": "market_cap",
    "Industry Group": "industry_group",
}

SCREENER_LIVE_COLS = [
    "company_name", "BSE Code", "nse_code", "ISIN Code", "industry_group", "Industry",
    "price", "market_cap", "pe", "historical_pe", "segment_pe", "Price to book value",
    "dividend_yield", "opm", "opm_latest_q", "opm_prev_year_q", "Debt to equity",
    "Operating profit growth", "interest_coverage", "roce", "Return on equity",
    "Promoter holding", "Unpledged promoter holding", "return_52w_vs_index", "fii_change",
    "dii_change", "Debt", "Working capital preceding year", "fii_holding", "dii_holding",
    "High price", "Sales growth", "Sales", "Profit after tax", "Enterprise Value", "EVEBITDA",
    "qoq_sales_growth", "qoq_profit_growth", "profit_growth_3y", "EPS last year",
    "EPS growth 5Years", "sales_growth_3y", "sales_growth_5y", "EPS", "Cfo by Pat",
    "PEG Ratio", "Average 5years dividend", "NPM last year", "No of Shareholder",
    "EPS growth 3Years", "EPS growth 7Years", "EPS growth 10Years", "profit_growth_5y",
    "return_1y", "return_3y", "dma_50", "dma_200", "RSI", "Number of equity shares",
    "fixed_asset_growth", "Return over 1month",
]
SCREENER_TEXT_COLS = {"company_name", "BSE Code", "nse_code", "ISIN Code", "industry_group", "Industry"}

BFSI_SEGMENTS = {
    "PSU Banks", "Private Banks", "Small Finance Banks", "NBFC - Large",
    "MSME Finance - Large", "MSME Finance - Mid", "MSME Finance - Small",
    "Microfinance & MSME", "Housing Finance", "Life Insurance",
    "General & Health Insurance", "Capital Markets - Large",
    "Broking & Wealth Management", "Exchanges & Ratings - Mid", "REITs",
    "Holding Companies",
}

# G + V peer params only (M now comes from momentum_scores).
# cc#506: "pe" added -- feeds the LIVE segment MEDIAN PE that replaces the stale/mean-based
# screener_raw.segment_pe ("Industry PE") as score_pe's segment benchmark. See _stock_dict().
PEER_PARAMS = [
    "sales_growth_5y", "sales_growth_3y", "profit_growth_5y", "profit_growth_3y",
    "qoq_sales_growth", "qoq_profit_growth", "opm", "opm_expansion", "fixed_asset_growth",
    "inst_holding_abs", "inst_holding_change", "roce", "interest_coverage",
    "dividend_yield", "potential_upside", "pe",
]


# ============================================================
# LABELS + VERDICT + PUNCHLINE
# Verdict framework (Arpit):
#   >= 8.0  -> Excellent
#   7.0-8.0 -> Good
#   6.0-7.0 -> Average
#   < 6.0   -> Weak
# ============================================================
def _label_growth(s):
    return "Excellent" if s >= 8 else "Healthy" if s >= 6.5 else "Average" if s >= 5 else "Weak"

def _label_value(s):
    return "Attractive" if s >= 7.5 else "Fair" if s >= 6 else "Premium" if s >= 4.5 else "Expensive"

def _label_momentum(s):
    return "Strong" if s >= 8 else "Positive" if s >= 6 else "Neutral" if s >= 4 else "Weak"

def _label_gvm(s):
    return ("Excellent" if s >= 8 else "Good" if s >= 7 else "Average" if s >= 6 else "Poor")

def _verdict(s):
    return ("Excellent" if s >= 8 else "Good" if s >= 7 else "Average" if s >= 6 else "Weak")

def _punchline(verd, g_lbl, v_lbl, m_lbl, gvm_lbl):
    action = {
        "Excellent": "It is highly recommended to Buy",
        "Good":      "It is recommended to Buy",
        "Average":   "It is advisable to Watch and Accumulate on dips",
        "Weak":      "It is advisable to Exit or Avoid",
    }[verd]
    return (f"{action} for medium to long term perspective, considering its "
            f"{g_lbl} Growth, {v_lbl} Valuation, {m_lbl} Momentum & {gvm_lbl} overall GVM Rating.")


# ============================================================
# LOAD: clean-replace screener_raw from uploaded rows (raw headers)
# ============================================================
def _sql_clean_replace_screener(rows: List[dict]) -> int:
    df = pd.DataFrame(rows)
    df = df.rename(columns={"NSE Code": "nse_code", "Name": "company_name"})
    df = df.rename(columns=SCREENER_COLUMNS)
    df = df[df["nse_code"].notna()].copy()
    df["nse_code"] = df["nse_code"].astype(str).str.strip()
    df = df[~df["nse_code"].isin(["", "nan"])].copy()
    df = df.drop_duplicates(subset="nse_code", keep="first").reset_index(drop=True)

    for c in df.columns:
        if c not in SCREENER_TEXT_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    cols = [c for c in SCREENER_LIVE_COLS if c in df.columns]
    placeholders = ", ".join(["%s"] * len(cols))
    colnames = ", ".join('"' + c + '"' for c in cols)

    def cell(c, v):
        if pd.isna(v):
            return None
        if c in SCREENER_TEXT_COLS:
            return str(v)
        try:
            f = float(v)
            return None if (np.isnan(f) or np.isinf(f)) else f
        except Exception:
            return None

    batch = [tuple(cell(c, r.get(c)) for c in cols) for _, r in df.iterrows()]

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM screener_raw")
        cur.executemany(f"INSERT INTO screener_raw ({colnames}) VALUES ({placeholders})", batch)
        conn.commit()
    return len(batch)


# ============================================================
# SECTOR RATINGS: mcap-weighted GVM per segment
# Runs after every gvm_scores update. Replaces sector_ratings table.
# ============================================================
def compute_sector_ratings(target_date: date) -> Dict:
    """
    Compute mcap-weighted GVM rating per segment from latest gvm_scores.
    Writes/replaces sector_ratings table.
    Returns: {segments, rows_written, date}
    """
    try:
        with _conn() as conn:
            df = pd.read_sql_query("""
                SELECT symbol, company_name, segment,
                       g_score, v_score, m_score, gvm_score, market_cap
                FROM gvm_scores
                WHERE gvm_score IS NOT NULL
            """, conn)

        if df.empty:
            return {"status": "warn", "message": "gvm_scores empty", "segments": 0}

        # mcap: use 1 as fallback if null (equal weight for mcap-unknown stocks)
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce").fillna(1)
        df["gvm_score"]  = pd.to_numeric(df["gvm_score"],  errors="coerce")
        df["g_score"]    = pd.to_numeric(df["g_score"],    errors="coerce")
        df["v_score"]    = pd.to_numeric(df["v_score"],    errors="coerce")
        df["m_score"]    = pd.to_numeric(df["m_score"],    errors="coerce")
        df = df.dropna(subset=["gvm_score"])

        rows = []
        for seg, grp in df.groupby("segment"):
            if seg in ("Unknown", "", None):
                continue
            total_mcap = grp["market_cap"].sum()
            if total_mcap <= 0:
                total_mcap = len(grp)  # fallback: equal weight

            def wt_avg(col):
                return round(float((grp[col] * grp["market_cap"]).sum() / total_mcap), 3)

            def simple_avg(col):
                return round(float(grp[col].mean()), 3)

            mcap_gvm = wt_avg("gvm_score")
            top_row = grp.nlargest(1, "gvm_score").iloc[0]

            rows.append((
                seg,
                int(len(grp)),
                mcap_gvm,
                wt_avg("g_score"),
                wt_avg("v_score"),
                wt_avg("m_score"),
                simple_avg("gvm_score"),
                round(float(total_mcap), 2),
                str(top_row["symbol"]),
                round(float(top_row["gvm_score"]), 2),
                _verdict(mcap_gvm),
                target_date,
            ))

        # Ensure table exists with correct schema
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_ratings (
                    id SERIAL PRIMARY KEY,
                    segment TEXT NOT NULL,
                    stocks_count INTEGER,
                    mcap_weighted_gvm NUMERIC,
                    weighted_g NUMERIC,
                    weighted_v NUMERIC,
                    weighted_m NUMERIC,
                    simple_avg_gvm NUMERIC,
                    total_mcap NUMERIC,
                    top_stock TEXT,
                    top_stock_gvm NUMERIC,
                    verdict TEXT,
                    score_date DATE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("DELETE FROM sector_ratings")
            cur.executemany("""
                INSERT INTO sector_ratings
                    (segment, stocks_count, mcap_weighted_gvm, weighted_g, weighted_v, weighted_m,
                     simple_avg_gvm, total_mcap, top_stock, top_stock_gvm, verdict, score_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, rows)
            conn.commit()

        log.info(f"sector_ratings: {len(rows)} segments written for {target_date}")
        return {"status": "ok", "segments": len(rows), "date": str(target_date)}

    except Exception as e:
        log.error(f"compute_sector_ratings failed: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================
# RECOMPUTE: read DB -> score G+V (screener) + M (momentum_scores) -> write tables
# ============================================================
def _load_merged_df(target_date: date) -> pd.DataFrame:
    with _conn() as conn:
        inp = pd.read_sql_query(
            "SELECT nse_code, company_name, market_cap, gvm_segment, fy27_growth FROM input_raw", conn
        )
        scr = pd.read_sql_query("SELECT * FROM screener_raw", conn)
        # latest momentum on/before target date
        mom = pd.read_sql_query(
            "SELECT DISTINCT ON (symbol) symbol, m_score FROM momentum_scores "
            "WHERE score_date <= %s ORDER BY symbol, score_date DESC",
            conn, params=(target_date,),
        )

    for d in (inp, scr):
        d["nse_code"] = d["nse_code"].astype(str).str.strip()
    mom["symbol"] = mom["symbol"].astype(str).str.strip()

    df = inp.merge(scr, on="nse_code", how="inner", suffixes=("", "_scr"))
    df = df.merge(mom.rename(columns={"symbol": "nse_code", "m_score": "m_score_daily"}),
                  on="nse_code", how="left")
    df["gvm_segment"] = df["gvm_segment"].astype(str).str.strip().replace({"nan": "Unknown", "": "Unknown"})

    if "opm_latest_q" in df and "opm_prev_year_q" in df:
        df["opm_expansion"] = (df["opm_latest_q"] - df["opm_prev_year_q"]) * 100
    else:
        df["opm_expansion"] = np.nan

    if "fii_holding" in df and "dii_holding" in df:
        df["inst_holding_abs"] = df["fii_holding"].fillna(0) + df["dii_holding"].fillna(0)
        df.loc[df["fii_holding"].isna() & df["dii_holding"].isna(), "inst_holding_abs"] = np.nan
    else:
        df["inst_holding_abs"] = np.nan

    if "fii_change" in df and "dii_change" in df:
        df["inst_holding_change"] = df["fii_change"].fillna(0) + df["dii_change"].fillna(0)
        df.loc[df["fii_change"].isna() & df["dii_change"].isna(), "inst_holding_change"] = np.nan
    else:
        df["inst_holding_change"] = np.nan

    def _pu(row):
        fy27, pe, hist = row.get("fy27_growth"), row.get("pe"), row.get("historical_pe")
        if pd.isna(fy27):
            return np.nan
        if fy27 == 0:
            return 0.0
        # hist_pe from screener_raw has dirty values (0.01, 0.14) that blow up the
        # pe/hist multiplier (task #41: SKFINDUS 44399%). Only trust hist_pe in a
        # sane band; else fall back to raw fy27 growth. Cap final upside at 150%.
        mult = (pe / hist) if (pd.notna(pe) and pd.notna(hist) and 5 < hist < 500) else 1.0
        return round(min(float(fy27) * mult, 150.0), 4)

    df["potential_upside"] = df.apply(_pu, axis=1)
    return df


def _peer_averages(df: pd.DataFrame) -> Dict:
    """cc#506: MEDIAN peers (was a 10-90 percentile trimmed MEAN). Median is inherently robust
    to outliers, so the old trim-then-mean two-step collapses to a single vals.median() -- same
    goal (don't let one extreme peer skew the benchmark), simpler mechanism. Every caller of
    these values (gvm_engine.py's score_relative/param_score/score_pe/etc.) takes peer input as
    a plain scalar, so this one change is sufficient to flow medians through the whole engine."""
    out = {}
    for seg, grp in df.groupby("gvm_segment"):
        avgs = {}
        for p in PEER_PARAMS:
            if p in grp.columns:
                vals = pd.to_numeric(grp[p], errors="coerce").dropna()
                avgs[p] = round(vals.median(), 4) if len(vals) else None
        out[seg] = avgs
    return out


def _stock_dict(row, peer_avgs):
    """G + V inputs only (M comes from momentum_scores separately)."""
    seg = row.get("gvm_segment", "Unknown")
    peers = peer_avgs.get(seg, {})

    def p(c):
        v = peers.get(c)
        return float(v) if v is not None else None

    def v(c):
        val = row.get(c)
        if val is None:
            return None
        try:
            f = float(val)
            return None if pd.isna(f) else f
        except Exception:
            return None

    return {
        "name": row.get("company_name", "Unknown"), "price": v("price") or 0,
        "segment": seg, "is_bfsi": seg in BFSI_SEGMENTS,
        "sales_growth_5y": v("sales_growth_5y"), "peer_sales_growth_5y": p("sales_growth_5y"),
        "sales_growth_3y": v("sales_growth_3y"), "peer_sales_growth_3y": p("sales_growth_3y"),
        "profit_growth_5y": v("profit_growth_5y"), "peer_profit_growth_5y": p("profit_growth_5y"),
        "profit_growth_3y": v("profit_growth_3y"), "peer_profit_growth_3y": p("profit_growth_3y"),
        "qoq_sales_growth": v("qoq_sales_growth"), "peer_qoq_sales_growth": p("qoq_sales_growth"),
        "qoq_profit_growth": v("qoq_profit_growth"), "peer_qoq_profit_growth": p("qoq_profit_growth"),
        "opm": v("opm"), "peer_opm": p("opm"),
        "opm_expansion": v("opm_expansion"), "peer_opm_expansion": p("opm_expansion"),
        "fixed_asset_growth": v("fixed_asset_growth"), "peer_fixed_asset_growth": p("fixed_asset_growth"),
        "inst_holding_abs": v("inst_holding_abs"), "peer_inst_holding_abs": p("inst_holding_abs"),
        "inst_holding_change": v("inst_holding_change"), "peer_inst_holding_change": p("inst_holding_change"),
        "roce": v("roce"), "peer_roce": p("roce"),
        "interest_coverage": v("interest_coverage"), "peer_interest_coverage": p("interest_coverage"),
        "dividend_yield": v("dividend_yield"), "peer_dividend_yield": p("dividend_yield"),
        # cc#506: segment_pe is now the LIVE segment MEDIAN pe (p("pe"), computed above from the
        # SAME peer set every other param uses) -- replaces the stale/mean-based screener_raw
        # "Industry PE" passthrough. historical_pe is unaffected (still each stock's own value).
        "pe": v("pe"), "historical_pe": v("historical_pe"), "segment_pe": p("pe"),
        "potential_upside": v("potential_upside"), "peer_potential_upside": p("potential_upside"),
    }


def sync_gvm_cache(cur) -> Dict:
    """cc#406: rebuild gvm_cache + peer_averages from the canonical gvm_scores (latest), and stamp
    cache_metadata fresh. The cache feeds /api/scorr/query (Max, CIO chips, query library); it lost
    its writer when GVM moved to the gvm_scores/gvm_history model, so it froze on 08-Jun. Runs as a
    post-step of recompute_gvm (same cursor/txn) and one-shot repair. No GVM math — pure projection."""
    cur.execute("DELETE FROM gvm_cache")
    cur.execute("""INSERT INTO gvm_cache (symbol, gvm_score, growth, value, momentum, segment, last_updated)
                   SELECT symbol, gvm_score, g_score, v_score, m_score, segment, NOW() FROM gvm_scores""")
    cur.execute("SELECT COUNT(*) FROM gvm_cache"); n_cache = cur.fetchone()[0]

    cur.execute("DELETE FROM peer_averages")
    cur.execute("""INSERT INTO peer_averages (segment, avg_gvm, avg_growth, avg_value, avg_momentum, stock_count, last_updated)
                   SELECT segment, ROUND(AVG(gvm_score)::numeric,2), ROUND(AVG(g_score)::numeric,2),
                          ROUND(AVG(v_score)::numeric,2), ROUND(AVG(m_score)::numeric,2), COUNT(*), NOW()
                   FROM gvm_scores WHERE segment IS NOT NULL GROUP BY segment""")
    cur.execute("SELECT COUNT(*) FROM peer_averages"); n_peers = cur.fetchone()[0]

    for k, cnt in (("gvm_cache", n_cache), ("peer_averages", n_peers)):
        cur.execute("""INSERT INTO cache_metadata (key, last_sync, stock_count, status)
                       VALUES (%s, NOW(), %s, 'ok')
                       ON CONFLICT (key) DO UPDATE SET last_sync=NOW(), stock_count=EXCLUDED.stock_count, status='ok'""",
                    (k, cnt))
    return {"gvm_cache": n_cache, "peer_averages": n_peers}


def recompute_gvm(target_date: Optional[date] = None, refresh_momentum: bool = True) -> Dict:
    target_date = target_date or date.today()

    # 1. Refresh daily momentum from raw_prices (price-driven M).
    mom_result = {"status": "skipped"}
    if refresh_momentum:
        try:
            mom_result = momentum_daily.compute_momentum(target_date)
        except Exception as e:
            log.error(f"momentum refresh failed: {e}")
            mom_result = {"status": "error", "message": str(e)}

    # 2. Merge fundamentals (screener) + daily momentum.
    df = _load_merged_df(target_date)
    if df.empty:
        return {"status": "warn", "message": "merge empty - check input_raw / screener_raw", "scored": 0,
                "momentum": mom_result}

    peer_avgs = _peer_averages(df)
    history_rows, latest_rows, errors, m_missing = [], [], 0, 0

    for _, row in df.iterrows():
        try:
            sd = _stock_dict(row, peer_avgs)
            g = api_g_score(sd)["score"]
            vv = api_v_score(sd)["score"]
            # M from daily momentum_scores; neutral 5.0 fallback if missing.
            m_raw = row.get("m_score_daily")
            if m_raw is None or (isinstance(m_raw, float) and pd.isna(m_raw)):
                m = 5.0
                m_missing += 1
            else:
                m = round(float(m_raw), 2)
            total = round((g + vv + m) / 3, 2)
            verd = _verdict(total)
            punch = _punchline(verd, _label_growth(g), _label_value(vv),
                               _label_momentum(m), _label_gvm(total))
            sym = str(row.get("nse_code", "")).strip()
            seg = row.get("gvm_segment", "Unknown")
            cname = row.get("company_name", sym)
            price = row.get("price")
            mcap = row.get("market_cap")
            price = float(price) if pd.notna(price) else None
            mcap = float(mcap) if pd.notna(mcap) else None
            # potential_upside is computed in _load_merged_df (fy27_growth × pe/hist)
            # and already drives the V score — also persist it for display (task #39).
            upside_val = row.get("potential_upside")
            upside_val = float(upside_val) if pd.notna(upside_val) else None

            history_rows.append((sym, target_date, g, vv, m, total, verd, seg))
            latest_rows.append((sym, cname, seg, price, g, vv, m, total, verd, punch, mcap, target_date, upside_val))
        except Exception as e:
            errors += 1
            log.warning(f"GVM score {row.get('nse_code','?')}: {e}")

    with _conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO gvm_history (symbol, score_date, g_score, v_score, m_score, gvm_score, verdict, segment)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                g_score=EXCLUDED.g_score, v_score=EXCLUDED.v_score, m_score=EXCLUDED.m_score,
                gvm_score=EXCLUDED.gvm_score, verdict=EXCLUDED.verdict, segment=EXCLUDED.segment
        """, history_rows)
        cur.execute("DELETE FROM gvm_scores")
        cur.executemany("""
            INSERT INTO gvm_scores
                (symbol, company_name, segment, price, g_score, v_score, m_score, gvm_score, verdict, punchline, market_cap, score_date, upside_raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, latest_rows)
        # cc#406: keep gvm_cache + peer_averages in lock-step with gvm_scores (same txn) so the
        # Max/query cache can never drift a month behind again.
        cache_result = sync_gvm_cache(cur)
        conn.commit()

    # 3. Recompute sector_ratings from the fresh gvm_scores — always runs after stock scoring.
    sector_result = compute_sector_ratings(target_date)

    return {
        "status": "ok", "score_date": str(target_date),
        "scored": len(history_rows), "errors": errors, "m_missing": m_missing,
        "momentum": mom_result,
        "sector_ratings": sector_result,
        "cache_sync": cache_result,
        "history_table": "gvm_history (appended)", "latest_table": "gvm_scores (replaced)",
    }


# ============================================================
# ROUTES
# ============================================================
@router.post("/api/admin/load_screener_json")
async def load_screener_json(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    body = await req.json()
    rows = body.get("rows")
    if not rows:
        raise HTTPException(400, "rows[] required")
    n = _sql_clean_replace_screener(rows)
    return {"status": "ok", "action": "clean_replace", "rows_loaded": n}


@router.post("/api/gvm/recompute")
def gvm_recompute(refresh_momentum: bool = True, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    return recompute_gvm(refresh_momentum=refresh_momentum)


@router.get("/api/gvm/history/{symbol}")
def gvm_history(symbol: str, days: int = 180):
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT score_date, g_score, v_score, m_score, gvm_score, verdict
            FROM gvm_history WHERE symbol = %s ORDER BY score_date DESC LIMIT %s
        """, (symbol.upper(), days))
        return {"symbol": symbol.upper(), "points": cur.fetchall()}
