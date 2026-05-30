"""
GVM Nightly Recompute - Scorr
===============================
Self-contained FastAPI router. Server-side GVM recompute driven entirely by
the LIVE DB tables (no CSV on disk). Reuses gvm_engine.py's 21-param scoring
untouched.

Workflow (event-driven, on screener upload):
  1. POST /api/admin/load_screener_json  {rows:[...]}  -> clean-replace screener_raw
  2. POST /api/gvm/recompute                            -> score all -> write:
         - gvm_history  (APPEND one dated row per stock; the trend table)
         - gvm_scores   (UPSERT latest snapshot; canonical read table)

Data sources (both already wide, normalized, joined on nse_code):
  - input_raw      : nse_code, company_name, market_cap, gvm_segment, fy27_growth
  - screener_raw   : nse_code, price, pe, roce, all 21-param inputs

gvm_scores canonical schema:
  symbol, company_name, segment, price, g_score, v_score, m_score, gvm_score,
  verdict, punchline, market_cap, score_date
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

from gvm_engine import api_gvm_score

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
# Mirrors screener_loader.SCREENER_COLUMNS so an uploaded raw export maps
# straight onto the live wide schema.
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

# Live screener_raw columns (order matters for INSERT). Excludes id, loaded_at.
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

PEER_PARAMS = [
    "sales_growth_5y", "sales_growth_3y", "profit_growth_5y", "profit_growth_3y",
    "qoq_sales_growth", "qoq_profit_growth", "opm", "opm_expansion", "fixed_asset_growth",
    "inst_holding_abs", "inst_holding_change", "roce", "interest_coverage",
    "dividend_yield", "potential_upside", "return_1y", "return_3y", "return_52w_vs_index",
]


# ============================================================
# LABELS + VERDICT + PUNCHLINE (from gvm_analytics.py)
# ============================================================
def _label_growth(s):
    return "Excellent" if s >= 8 else "Healthy" if s >= 6.5 else "Average" if s >= 5 else "Weak"

def _label_value(s):
    return "Attractive" if s >= 7.5 else "Fair" if s >= 6 else "Premium" if s >= 4.5 else "Expensive"

def _label_momentum(s):
    return "Strong" if s >= 8 else "Positive" if s >= 6 else "Neutral" if s >= 4 else "Weak"

def _label_gvm(s):
    return ("Excellent" if s >= 8 else "Good" if s >= 7 else "Average" if s >= 6
            else "Below Average" if s >= 5 else "Poor")

def _verdict(s):
    return ("Strong Buy" if s >= 8 else "Buy" if s >= 7 else "Accumulate" if s >= 6
            else "Wait & Watch" if s >= 5 else "Avoid")

def _punchline(verd, g_lbl, v_lbl, m_lbl, gvm_lbl):
    action = {
        "Strong Buy": "It is highly recommended to Buy",
        "Buy": "It is recommended to Buy",
        "Accumulate": "It is advisable to Accumulate",
        "Wait & Watch": "It is advisable to Wait & Watch",
        "Avoid": "It is advisable to Avoid",
    }[verd]
    return (f"{action} for medium to long term perspective, considering its "
            f"{g_lbl} Growth, {v_lbl} Valuation, {m_lbl} Momentum & {gvm_lbl} overall GVM Rating.")


# ============================================================
# LOAD: clean-replace screener_raw from uploaded rows (raw headers)
# ============================================================
def _sql_clean_replace_screener(rows: List[dict]) -> int:
    """rows are dicts keyed by RAW Screener headers. Rename, clean, replace."""
    df = pd.DataFrame(rows)
    df = df.rename(columns={"NSE Code": "nse_code", "Name": "company_name"})
    df = df.rename(columns=SCREENER_COLUMNS)
    df = df[df["nse_code"].notna()].copy()
    df["nse_code"] = df["nse_code"].astype(str).str.strip()
    df = df[~df["nse_code"].isin(["", "nan"])].copy()
    df = df.drop_duplicates(subset="nse_code", keep="first").reset_index(drop=True)

    # numeric coercion for non-text cols present
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
# RECOMPUTE: read DB -> score -> write gvm_history + gvm_scores
# ============================================================
def _load_merged_df() -> pd.DataFrame:
    with _conn() as conn:
        inp = pd.read_sql_query(
            "SELECT nse_code, company_name, market_cap, gvm_segment, fy27_growth FROM input_raw", conn
        )
        scr = pd.read_sql_query("SELECT * FROM screener_raw", conn)

    for d in (inp, scr):
        d["nse_code"] = d["nse_code"].astype(str).str.strip()

    # input_raw is master for segment + fy27; screener_raw is fundamentals.
    df = inp.merge(scr, on="nse_code", how="inner", suffixes=("", "_scr"))
    df["gvm_segment"] = df["gvm_segment"].astype(str).str.strip().replace({"nan": "Unknown", "": "Unknown"})

    # derived inputs the engine expects
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
        mult = (pe / hist) if (pd.notna(pe) and pd.notna(hist) and hist > 0) else 1.0
        return round(float(fy27) * mult, 4)

    df["potential_upside"] = df.apply(_pu, axis=1)
    return df


def _peer_averages(df: pd.DataFrame) -> Dict:
    out = {}
    for seg, grp in df.groupby("gvm_segment"):
        avgs = {}
        for p in PEER_PARAMS:
            if p in grp.columns:
                vals = pd.to_numeric(grp[p], errors="coerce").dropna()
                if len(vals) >= 3:
                    lo, hi = vals.quantile(0.10), vals.quantile(0.90)
                    trimmed = vals[(vals >= lo) & (vals <= hi)]
                    avgs[p] = round(trimmed.mean(), 4) if len(trimmed) else round(vals.mean(), 4)
                elif len(vals):
                    avgs[p] = round(vals.mean(), 4)
                else:
                    avgs[p] = None
        out[seg] = avgs
    return out


def _stock_dict(row, peer_avgs):
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
        "pe": v("pe"), "historical_pe": v("historical_pe"), "segment_pe": v("segment_pe"),
        "potential_upside": v("potential_upside"), "peer_potential_upside": p("potential_upside"),
        "return_1y": v("return_1y"), "peer_return_1y": p("return_1y"),
        "return_3y": v("return_3y"), "peer_return_3y": p("return_3y"),
        "dma_50": v("dma_50"), "dma_200": v("dma_200"),
        "return_52w_vs_index": v("return_52w_vs_index"), "peer_return_52w_vs_index": p("return_52w_vs_index"),
    }


def recompute_gvm(target_date: Optional[date] = None) -> Dict:
    target_date = target_date or date.today()
    df = _load_merged_df()
    if df.empty:
        return {"status": "warn", "message": "merge empty - check input_raw / screener_raw", "scored": 0}

    peer_avgs = _peer_averages(df)
    history_rows, latest_rows, errors = [], [], 0

    for _, row in df.iterrows():
        try:
            sd = _stock_dict(row, peer_avgs)
            r = api_gvm_score(sd)
            g, vv, m, total = r["G_score"], r["V_score"], r["M_score"], r["GVM_score"]
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

            history_rows.append((sym, target_date, g, vv, m, total, verd, seg))
            latest_rows.append((sym, cname, seg, price, g, vv, m, total, verd, punch, mcap, target_date))
        except Exception as e:
            errors += 1
            log.warning(f"GVM score {row.get('nse_code','?')}: {e}")

    with _conn() as conn, conn.cursor() as cur:
        # APPEND to gvm_history (re-runnable same day via unique upsert)
        cur.executemany("""
            INSERT INTO gvm_history (symbol, score_date, g_score, v_score, m_score, gvm_score, verdict, segment)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                g_score=EXCLUDED.g_score, v_score=EXCLUDED.v_score, m_score=EXCLUDED.m_score,
                gvm_score=EXCLUDED.gvm_score, verdict=EXCLUDED.verdict, segment=EXCLUDED.segment
        """, history_rows)
        # REPLACE latest snapshot in gvm_scores
        cur.execute("DELETE FROM gvm_scores")
        cur.executemany("""
            INSERT INTO gvm_scores
                (symbol, company_name, segment, price, g_score, v_score, m_score, gvm_score, verdict, punchline, market_cap, score_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, latest_rows)
        conn.commit()

    return {
        "status": "ok", "score_date": str(target_date),
        "scored": len(history_rows), "errors": errors,
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
def gvm_recompute(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    return recompute_gvm()


@router.get("/api/gvm/history/{symbol}")
def gvm_history(symbol: str, days: int = 180):
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT score_date, g_score, v_score, m_score, gvm_score, verdict
            FROM gvm_history WHERE symbol = %s ORDER BY score_date DESC LIMIT %s
        """, (symbol.upper(), days))
        return {"symbol": symbol.upper(), "points": cur.fetchall()}
