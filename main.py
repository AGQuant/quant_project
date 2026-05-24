from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import os
import psycopg
import urllib.parse
import secrets
import logging
import subprocess
import json
import uuid
import httpx
import io
from datetime import date, datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from gvm_engine import (
    api_sales_growth_5y, api_sales_growth_3y,
    api_profit_growth_5y, api_profit_growth_3y,
    api_qoq_sales_growth, api_qoq_profit_growth,
    api_opm, api_opm_expansion, api_fixed_asset_growth,
    api_inst_holding_abs, api_inst_holding_change,
    api_roce, api_interest_coverage, api_dividend_yield,
    api_pe_ratio, api_potential_upside,
    api_return_1y, api_return_3y,
    api_dma50, api_dma200, api_return_52w_vs_index,
    api_g_score, api_v_score, api_m_score, api_gvm_score
)

BASE_URL = "https://quantproject-production.up.railway.app"
_issued_tokens: set = set()


# ============================================
# DB CONNECTION HELPER
# ============================================

def get_db_conn():
    db_url = os.environ.get("DATABASE_URL", "")
    parsed = urllib.parse.urlparse(db_url)
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip('/'),
        user=parsed.username,
        password=parsed.password
    )


def create_tables():
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute("""CREATE TABLE IF NOT EXISTS gvm_scores (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            company_name VARCHAR(200),
            segment VARCHAR(100),
            rank INT,
            price DECIMAL(12,2),
            g_score DECIMAL(5,2), v_score DECIMAL(5,2), m_score DECIMAL(5,2), gvm_score DECIMAL(5,2),
            growth_label VARCHAR(50), value_label VARCHAR(50), momentum_label VARCHAR(50),
            gvm_overall_label VARCHAR(50), verdict VARCHAR(50), punchline TEXT,
            sales_5y_raw DECIMAL(15,2), sales_5y_peer DECIMAL(15,2), sales_5y_rating DECIMAL(4,1),
            sales_3y_raw DECIMAL(15,2), sales_3y_peer DECIMAL(15,2), sales_3y_rating DECIMAL(4,1),
            profit_5y_raw DECIMAL(15,2), profit_5y_peer DECIMAL(15,2), profit_5y_rating DECIMAL(4,1),
            profit_3y_raw DECIMAL(15,2), profit_3y_peer DECIMAL(15,2), profit_3y_rating DECIMAL(4,1),
            qoq_sales_raw DECIMAL(15,2), qoq_sales_peer DECIMAL(15,2), qoq_sales_rating DECIMAL(4,1),
            qoq_profit_raw DECIMAL(15,2), qoq_profit_peer DECIMAL(15,2), qoq_profit_rating DECIMAL(4,1),
            opm_raw DECIMAL(15,2), opm_peer DECIMAL(15,2), opm_rating DECIMAL(4,1),
            opm_exp_raw DECIMAL(15,2), opm_exp_peer DECIMAL(15,2), opm_exp_rating DECIMAL(4,1),
            fa_growth_raw DECIMAL(15,2), fa_growth_peer DECIMAL(15,2), fa_growth_rating DECIMAL(4,1),
            promoter_raw DECIMAL(15,2), promoter_rating DECIMAL(4,1),
            inst_change_raw DECIMAL(15,2), inst_change_peer DECIMAL(15,2), inst_change_rating DECIMAL(4,1),
            roce_raw DECIMAL(15,2), roce_peer DECIMAL(15,2), roce_rating DECIMAL(4,1),
            int_cov_raw DECIMAL(15,2), int_cov_peer DECIMAL(15,2), int_cov_rating DECIMAL(4,1),
            div_yield_raw DECIMAL(15,2), div_yield_peer DECIMAL(15,2), div_yield_rating DECIMAL(4,1),
            pe_raw DECIMAL(15,2), pe_peer DECIMAL(15,2), pe_rating DECIMAL(4,1),
            upside_raw DECIMAL(15,2), upside_peer DECIMAL(15,2), upside_rating DECIMAL(4,1),
            ret_1y_raw DECIMAL(15,2), ret_1y_peer DECIMAL(15,2), ret_1y_rating DECIMAL(4,1),
            ret_3y_raw DECIMAL(15,2), ret_3y_peer DECIMAL(15,2), ret_3y_rating DECIMAL(4,1),
            dma_50_raw DECIMAL(15,2), dma_50_peer DECIMAL(15,2), dma_50_rating DECIMAL(4,1),
            dma_200_raw DECIMAL(15,2), dma_200_peer DECIMAL(15,2), dma_200_rating DECIMAL(4,1),
            ret_52w_idx_raw DECIMAL(15,2), ret_52w_idx_peer DECIMAL(15,2), ret_52w_idx_rating DECIMAL(4,1),
            score_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(symbol, score_date)
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS raw_prices (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(50) NOT NULL,
            price_date DATE NOT NULL,
            open DECIMAL(15,2), high DECIMAL(15,2),
            low DECIMAL(15,2), close DECIMAL(15,2),
            adjusted_close DECIMAL(15,2),
            volume BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, price_date)
        )""")
        cursor.execute("ALTER TABLE raw_prices ADD COLUMN IF NOT EXISTS adjusted_close DECIMAL(15,2);")
        cursor.execute("""CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY, symbol VARCHAR(20),
            signal_type VARCHAR(50), created_at TIMESTAMP DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, email VARCHAR(200) UNIQUE,
            created_at TIMESTAMP DEFAULT NOW())""")

        # DAY 32 ADDITIONS — sector_ratings + momentum_scores
        cursor.execute("""CREATE TABLE IF NOT EXISTS sector_ratings (
            id SERIAL PRIMARY KEY,
            segment TEXT NOT NULL,
            stocks_count INT,
            mcap_weighted_gvm NUMERIC,
            weighted_g NUMERIC,
            weighted_v NUMERIC,
            weighted_m NUMERIC,
            simple_avg_gvm NUMERIC,
            total_mcap NUMERIC,
            top_stock TEXT,
            top_stock_gvm NUMERIC,
            verdict TEXT,
            score_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(segment, score_date)
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS momentum_scores (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            score_date DATE NOT NULL,
            latest_price NUMERIC,
            ret_1y NUMERIC,
            ret_3y NUMERIC,
            dma_50 NUMERIC,
            dma_200 NUMERIC,
            ret_52w_vs_index NUMERIC,
            ret_1y_rating NUMERIC,
            ret_3y_rating NUMERIC,
            dma_50_rating NUMERIC,
            dma_200_rating NUMERIC,
            ret_52w_idx_rating NUMERIC,
            m_score NUMERIC,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(symbol, score_date)
        )""")

        conn.commit()
        cursor.close()
        conn.close()
        print("Tables ready")
    except Exception as e:
        print(f"Table creation error: {e}")


app = FastAPI(
    title="Project Quant — Trading API",
    description="Proprietary GVM quant scoring engine — 29 APIs + MCP + Sector + Momentum",
    version="1.1.0",
    redirect_slashes=False
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# OAUTH 2.1 — Required by Claude.ai for MCP
# ============================================

@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/authorize",
        "token_endpoint": f"{BASE_URL}/token",
        "registration_endpoint": f"{BASE_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"]
    }

@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return {"resource": BASE_URL, "authorization_servers": [BASE_URL]}

@app.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource_mcp():
    return {"resource": f"{BASE_URL}/mcp", "authorization_servers": [BASE_URL]}

@app.post("/register")
async def register_client(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return {
        "client_id": f"claude-{secrets.token_hex(8)}",
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none"
    }

@app.get("/authorize")
async def authorize(
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256"
):
    code = secrets.token_hex(16)
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=redirect_uri + sep + urllib.parse.urlencode(params),
        status_code=302
    )

@app.post("/token")
async def issue_token(request: Request):
    access_token = secrets.token_hex(32)
    _issued_tokens.add(access_token)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 86400,
        "scope": "mcp"
    }


# ============================================
# ADMIN ENDPOINTS — Drive-to-DB loaders
# ============================================

def _drive_download(file_id: str) -> bytes:
    """Download a public Google Drive file by ID."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        r = client.get(url)
        if r.status_code != 200:
            raise Exception(f"Drive fetch failed: HTTP {r.status_code}")
        return r.content


@app.post("/api/admin/load_input_from_drive")
async def load_input_from_drive(request: Request):
    """
    POST {"file_id": "1JVroa-OUM1mKBXcEjlzjrI0AJhsZ2lTo"}
    Loads input.csv from Drive into input_raw table.
    Requires file to be 'Anyone with link - Viewer'.
    """
    try:
        import pandas as pd
        body = await request.json()
        file_id = body.get("file_id", "")
        if not file_id:
            return {"error": "file_id required"}

        content = _drive_download(file_id)
        df = pd.read_csv(io.BytesIO(content), dtype=str)

        # Clean
        df = df[df['NSE Code'].notna()].copy()
        df['NSE Code'] = df['NSE Code'].astype(str).str.strip()
        df = df[~df['NSE Code'].isin(['', 'nan'])]
        df = df.drop_duplicates(subset='NSE Code', keep='first').reset_index(drop=True)

        def clean(v):
            if v is None or (isinstance(v, float) and pd.isna(v)): return None
            s = str(v).strip()
            return s if s and s.lower() != 'nan' else None

        def num(v):
            try:
                if v is None or pd.isna(v): return None
                return float(v)
            except (ValueError, TypeError): return None

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS input_raw (
            id SERIAL PRIMARY KEY,
            nse_code TEXT, company_name TEXT, bse_code TEXT, cmot_code TEXT,
            market_cap NUMERIC, overview TEXT, key_takeaway TEXT,
            gvm_segment TEXT, fy27_growth NUMERIC, finkhoz_rating NUMERIC,
            loaded_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("DELETE FROM input_raw")

        inserted = 0
        for _, row in df.iterrows():
            cur.execute("""
                INSERT INTO input_raw (nse_code, company_name, bse_code, cmot_code, market_cap, overview, key_takeaway, gvm_segment, fy27_growth, finkhoz_rating)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                clean(row.get('NSE Code')), clean(row.get('Name')),
                clean(row.get('BSE Code')), clean(row.get('CMOT Code')),
                num(row.get('Market Capitalization')),
                clean(row.get('Overview')), clean(row.get('Key Takeways')),
                clean(row.get('Segment')),
                num(row.get('FY27 EPS Est.')), num(row.get('Finkhoz Rating'))
            ))
            inserted += 1

        conn.commit()
        cur.close(); conn.close()
        return {"status": "ok", "rows_loaded": inserted, "table": "input_raw"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/load_screener_from_drive")
async def load_screener_from_drive(request: Request):
    """
    POST {"file_id": "1KyKcNww_yne1RXs9MQ9dijBRiW-IJVU4"}
    Loads screener.csv from Drive into screener_raw table.
    """
    try:
        import pandas as pd
        body = await request.json()
        file_id = body.get("file_id", "")
        if not file_id:
            return {"error": "file_id required"}

        content = _drive_download(file_id)
        df = pd.read_csv(io.BytesIO(content), dtype=str)

        # Use the same column mapping as screener_loader.py
        SCREENER_COLUMNS = {
            "Current Price": "price",
            "Sales growth 5Years": "sales_growth_5y",
            "Sales growth 3Years": "sales_growth_3y",
            "Profit growth 5Years": "profit_growth_5y",
            "Profit growth 3Years": "profit_growth_3y",
            "YOY Quarterly sales growth": "qoq_sales_growth",
            "YOY Quarterly profit growth": "qoq_profit_growth",
            "OPM": "opm",
            "OPM latest quarter": "opm_latest_q",
            "OPM preceding year quarter": "opm_prev_year_q",
            "Fixed Asset Growth": "fixed_asset_growth",
            "FII holding": "fii_holding",
            "DII holding": "dii_holding",
            "Change in FII holding": "fii_change",
            "Change in DII holding": "dii_change",
            "Return on capital employed": "roce",
            "Interest Coverage Ratio": "interest_coverage",
            "Dividend yield": "dividend_yield",
            "Price to Earning": "pe",
            "Historical PE 10Years": "historical_pe",
            "Industry PE": "segment_pe",
            "Return over 1year": "return_1y",
            "Return over 3years": "return_3y",
            "DMA 50": "dma_50",
            "DMA 200": "dma_200",
            "52w Index": "return_52w_vs_index",
            "Market Capitalization": "market_cap",
            "Industry Group": "industry_group",
        }

        df = df.rename(columns={"NSE Code": "nse_code", "Name": "company_name"})
        df = df.rename(columns=SCREENER_COLUMNS)

        df = df[df['nse_code'].notna()].copy()
        df['nse_code'] = df['nse_code'].astype(str).str.strip()
        df = df[~df['nse_code'].isin(['', 'nan'])]
        df = df.drop_duplicates(subset='nse_code', keep='first').reset_index(drop=True)

        TEXT_COLS = {"nse_code", "company_name", "industry_group", "Industry", "ISIN Code", "BSE Code"}

        for c in df.columns:
            if c not in TEXT_COLS:
                df[c] = pd.to_numeric(df[c], errors='coerce')

        for c in TEXT_COLS:
            if c in df.columns:
                df[c] = df[c].astype(str).replace({"nan": None, "None": None, "": None})

        def pg_type(col):
            return "TEXT" if col in TEXT_COLS else "NUMERIC"

        cols_def = ",\n  ".join(f'"{c}" {pg_type(c)}' for c in df.columns)

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS screener_raw")
        cur.execute(f"""CREATE TABLE screener_raw (
            id SERIAL PRIMARY KEY,
            {cols_def},
            loaded_at TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()

        cols = list(df.columns)
        placeholders = ", ".join(["%s"] * len(cols))
        col_names = ", ".join([f'"{c}"' for c in cols])
        insert_sql = f"INSERT INTO screener_raw ({col_names}) VALUES ({placeholders})"

        inserted = 0
        batch = []
        for _, row in df.iterrows():
            vals = []
            for c in cols:
                v = row[c]
                try:
                    if v is None:
                        vals.append(None)
                    elif c in TEXT_COLS:
                        s = str(v).strip()
                        vals.append(s if s and s.lower() != "nan" else None)
                    elif pd.isna(v):
                        vals.append(None)
                    else:
                        vals.append(float(v))
                except (ValueError, TypeError):
                    vals.append(None)
            batch.append(vals)
            if len(batch) == 100:
                cur.executemany(insert_sql, batch)
                conn.commit()
                inserted += len(batch)
                batch = []
        if batch:
            cur.executemany(insert_sql, batch)
            conn.commit()
            inserted += len(batch)

        cur.close(); conn.close()
        return {"status": "ok", "rows_loaded": inserted, "table": "screener_raw"}
    except Exception as e:
        return {"error": str(e)}


# ============================================
# DAY 32 — ADMIN REFRESH ENDPOINTS
# ============================================

@app.post("/api/admin/refresh_sector_ratings")
async def refresh_sector_ratings():
    """
    Recompute sector_ratings from latest gvm_scores. mcap-weighted GVM/G/V/M.
    Call after daily GVM update.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sector_ratings
                (segment, stocks_count, mcap_weighted_gvm, weighted_g, weighted_v,
                 weighted_m, simple_avg_gvm, total_mcap, top_stock, top_stock_gvm,
                 verdict, score_date)
            SELECT s.segment, s.stocks, s.w_gvm, s.w_g, s.w_v, s.w_m,
                   s.avg_gvm, s.total_mcap, t.symbol, t.gvm_score,
                   CASE
                       WHEN s.w_gvm >= 7.0 THEN 'Strong'
                       WHEN s.w_gvm >= 6.5 THEN 'Buy'
                       WHEN s.w_gvm >= 6.0 THEN 'Watch'
                       ELSE 'Avoid'
                   END,
                   s.score_date
            FROM (
                SELECT segment, COUNT(*) AS stocks,
                       ROUND(SUM(gvm_score * market_cap) / NULLIF(SUM(market_cap), 0), 2) AS w_gvm,
                       ROUND(SUM(g_score * market_cap) / NULLIF(SUM(market_cap), 0), 2) AS w_g,
                       ROUND(SUM(v_score * market_cap) / NULLIF(SUM(market_cap), 0), 2) AS w_v,
                       ROUND(SUM(m_score * market_cap) / NULLIF(SUM(market_cap), 0), 2) AS w_m,
                       ROUND(AVG(gvm_score)::numeric, 2) AS avg_gvm,
                       SUM(market_cap) AS total_mcap,
                       MAX(score_date) AS score_date
                FROM gvm_scores
                WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
                AND market_cap IS NOT NULL
                GROUP BY segment
                HAVING COUNT(*) >= 3
            ) s
            LEFT JOIN LATERAL (
                SELECT symbol, gvm_score
                FROM gvm_scores
                WHERE segment = s.segment AND score_date = s.score_date
                ORDER BY gvm_score DESC LIMIT 1
            ) t ON true
            ON CONFLICT (segment, score_date) DO UPDATE SET
                stocks_count = EXCLUDED.stocks_count,
                mcap_weighted_gvm = EXCLUDED.mcap_weighted_gvm,
                weighted_g = EXCLUDED.weighted_g,
                weighted_v = EXCLUDED.weighted_v,
                weighted_m = EXCLUDED.weighted_m,
                simple_avg_gvm = EXCLUDED.simple_avg_gvm,
                total_mcap = EXCLUDED.total_mcap,
                top_stock = EXCLUDED.top_stock,
                top_stock_gvm = EXCLUDED.top_stock_gvm,
                verdict = EXCLUDED.verdict
        """)
        conn.commit()
        cur.execute("""
            SELECT COUNT(*) FROM sector_ratings
            WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)
        """)
        count = cur.fetchone()[0]
        cur.close(); conn.close()
        return {"status": "ok", "sectors_refreshed": count}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/refresh_momentum")
async def refresh_momentum():
    """
    Recompute momentum_scores from raw_prices. 5-param M score.
    Call after daily Yahoo OHLC update.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # Compute raw momentum values
        cur.execute("""
            INSERT INTO momentum_scores
                (symbol, score_date, latest_price, ret_1y, ret_3y,
                 dma_50, dma_200, ret_52w_vs_index)
            WITH d AS (SELECT MAX(price_date) AS dt FROM raw_prices),
            nifty_1y AS (
                SELECT (l.close / y.close - 1) * 100 AS pct
                FROM (SELECT close FROM raw_prices
                      WHERE symbol='NIFTY50' AND price_date=(SELECT dt FROM d)) l
                CROSS JOIN (SELECT close FROM raw_prices
                            WHERE symbol='NIFTY50'
                            AND price_date <= (SELECT dt FROM d) - INTERVAL '365 days'
                            ORDER BY price_date DESC LIMIT 1) y
            ),
            latest_px AS (
                SELECT symbol, close AS latest_close FROM raw_prices
                WHERE price_date = (SELECT dt FROM d)
                AND symbol NOT IN ('NIFTY50','BANKNIFTY')
            ),
            px_1y AS (
                SELECT DISTINCT ON (symbol) symbol, close AS close_1y
                FROM raw_prices
                WHERE price_date <= (SELECT dt FROM d) - INTERVAL '365 days'
                AND symbol NOT IN ('NIFTY50','BANKNIFTY')
                ORDER BY symbol, price_date DESC
            ),
            px_3y AS (
                SELECT DISTINCT ON (symbol) symbol, close AS close_3y
                FROM raw_prices
                WHERE price_date <= (SELECT dt FROM d) - INTERVAL '1095 days'
                AND symbol NOT IN ('NIFTY50','BANKNIFTY')
                ORDER BY symbol, price_date DESC
            ),
            dma50 AS (
                SELECT symbol, AVG(close) AS dma50_val
                FROM (SELECT symbol, close,
                             ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                      FROM raw_prices
                      WHERE symbol NOT IN ('NIFTY50','BANKNIFTY')) x
                WHERE rn <= 50 GROUP BY symbol
            ),
            dma200 AS (
                SELECT symbol, AVG(close) AS dma200_val
                FROM (SELECT symbol, close,
                             ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                      FROM raw_prices
                      WHERE symbol NOT IN ('NIFTY50','BANKNIFTY')) x
                WHERE rn <= 200 GROUP BY symbol
            )
            SELECT lp.symbol, (SELECT dt FROM d), lp.latest_close,
                   ROUND(((lp.latest_close / p1.close_1y - 1) * 100)::numeric, 2),
                   ROUND(((lp.latest_close / p3.close_3y - 1) * 100)::numeric, 2),
                   ROUND(((lp.latest_close / d50.dma50_val - 1) * 100)::numeric, 2),
                   ROUND(((lp.latest_close / d200.dma200_val - 1) * 100)::numeric, 2),
                   ROUND((((lp.latest_close / p1.close_1y - 1) * 100) -
                          (SELECT pct FROM nifty_1y))::numeric, 2)
            FROM latest_px lp
            JOIN px_1y p1 USING (symbol)
            LEFT JOIN px_3y p3 USING (symbol)
            LEFT JOIN dma50 d50 USING (symbol)
            LEFT JOIN dma200 d200 USING (symbol)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                latest_price = EXCLUDED.latest_price,
                ret_1y = EXCLUDED.ret_1y,
                ret_3y = EXCLUDED.ret_3y,
                dma_50 = EXCLUDED.dma_50,
                dma_200 = EXCLUDED.dma_200,
                ret_52w_vs_index = EXCLUDED.ret_52w_vs_index
        """)

        # Apply ratings (2.5/5/7.5/10 scale)
        cur.execute("""
            UPDATE momentum_scores SET
                ret_1y_rating = CASE
                    WHEN ret_1y > 15 THEN 10
                    WHEN ret_1y >= 5 THEN 7.5
                    WHEN ret_1y >= 0 THEN 5 ELSE 2.5 END,
                ret_3y_rating = CASE
                    WHEN ret_3y IS NULL THEN 5
                    WHEN ret_3y > 60 THEN 10
                    WHEN ret_3y >= 30 THEN 7.5
                    WHEN ret_3y >= 0 THEN 5 ELSE 2.5 END,
                dma_50_rating = CASE
                    WHEN dma_50 > 10 THEN 10
                    WHEN dma_50 >= 3 THEN 7.5
                    WHEN dma_50 >= 0 THEN 5 ELSE 2.5 END,
                dma_200_rating = CASE
                    WHEN dma_200 > 25 THEN 10
                    WHEN dma_200 >= 10 THEN 7.5
                    WHEN dma_200 >= 0 THEN 5 ELSE 2.5 END,
                ret_52w_idx_rating = CASE
                    WHEN ret_52w_vs_index > 15 THEN 10
                    WHEN ret_52w_vs_index >= 5 THEN 7.5
                    WHEN ret_52w_vs_index >= 0 THEN 5 ELSE 2.5 END
            WHERE score_date = (SELECT MAX(score_date) FROM momentum_scores)
        """)

        # Composite M score
        cur.execute("""
            UPDATE momentum_scores SET
                m_score = ROUND(((ret_1y_rating + ret_3y_rating + dma_50_rating +
                                  dma_200_rating + ret_52w_idx_rating) / 5)::numeric, 2)
            WHERE score_date = (SELECT MAX(score_date) FROM momentum_scores)
        """)

        conn.commit()
        cur.execute("""
            SELECT COUNT(*) FROM momentum_scores
            WHERE score_date = (SELECT MAX(score_date) FROM momentum_scores)
        """)
        count = cur.fetchone()[0]
        cur.close(); conn.close()
        return {"status": "ok", "stocks_refreshed": count}
    except Exception as e:
        return {"error": str(e)}


# ============================================
# MCP TOOL CALLER — calls our own REST APIs
# ============================================

async def _call_tool(name: str, args: dict) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        if name == "get_gvm":
            r = await client.get(f"{BASE_URL}/api/gvm/{args.get('symbol', '')}")
            return r.json()
        elif name == "get_top_stocks":
            p = {"n": args.get("n", 20)}
            if args.get("verdict"):
                p["verdict"] = args["verdict"]
            r = await client.get(f"{BASE_URL}/api/gvm/top", params=p)
            return r.json()
        elif name == "get_sector":
            r = await client.get(f"{BASE_URL}/api/gvm/sector",
                                 params={"segment": args.get("segment", ""), "n": args.get("n", 20)})
            return r.json()
        elif name == "get_filter":
            p = {"min_score": args.get("min_score", 7.0),
                 "max_score": args.get("max_score", 10.0),
                 "n": args.get("n", 50)}
            if args.get("verdict"):
                p["verdict"] = args["verdict"]
            r = await client.get(f"{BASE_URL}/api/gvm/filter", params=p)
            return r.json()
        elif name == "run_sql":
            query = args.get("query", "")
            params = args.get("params", [])
            blocked = ["drop table", "delete from", "truncate"]
            if any(b in query.lower() for b in blocked):
                return {"error": "Blocked. DROP/DELETE/TRUNCATE not allowed."}
            try:
                from psycopg.rows import dict_row
                db_url = os.environ.get("DATABASE_URL", "")
                with psycopg.connect(db_url, row_factory=dict_row) as conn:
                    with conn.cursor() as cur:
                        cur.execute(query, params or [])
                        if cur.description is None:
                            conn.commit()
                            return {"status": "ok", "message": "Query executed."}
                        rows = cur.fetchall()
                        return {
                            "status": "ok",
                            "rows": len(rows),
                            "columns": [d.name for d in cur.description],
                            "data": [dict(r) for r in rows]
                        }
            except Exception as e:
                return {"error": str(e)}
        elif name == "load_input_from_drive":
            file_id = args.get("file_id", "")
            if not file_id:
                return {"error": "file_id required"}
            r = await client.post(f"{BASE_URL}/api/admin/load_input_from_drive", json={"file_id": file_id})
            return r.json()
        elif name == "load_screener_from_drive":
            file_id = args.get("file_id", "")
            if not file_id:
                return {"error": "file_id required"}
            r = await client.post(f"{BASE_URL}/api/admin/load_screener_from_drive", json={"file_id": file_id})
            return r.json()
        # DAY 32 NEW TOOLS
        elif name == "get_sector_rating":
            segment = args.get("segment")
            n = args.get("n", 20)
            if segment:
                r = await client.get(f"{BASE_URL}/api/sector/rating/{segment}")
            else:
                r = await client.get(f"{BASE_URL}/api/sector/rating", params={"n": n})
            return r.json()
        elif name == "get_momentum":
            symbol = args.get("symbol")
            if symbol:
                r = await client.get(f"{BASE_URL}/api/momentum/stock/{symbol}")
            else:
                p = {"n": args.get("n", 20), "min_score": args.get("min_score", 7.0)}
                r = await client.get(f"{BASE_URL}/api/momentum/top", params=p)
            return r.json()
        elif name == "health_feeds":
            r = await client.get(f"{BASE_URL}/api/health/feeds")
            return r.json()
        return {"error": f"Unknown tool: {name}"}


# ============================================
# MCP SERVER — Direct JSON-RPC implementation
# ============================================

MCP_TOOLS = [
    {
        "name": "get_gvm",
        "description": "Fetch full GVM score for a stock from Railway DB. Pass NSE symbol e.g. RELIANCE, INFY, HDFCBANK, CARYSIL",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "NSE stock symbol"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_top_stocks",
        "description": "Get top N stocks ranked by GVM score from Railway DB. verdict options: 'Strong Buy', 'Buy', 'Accumulate', 'Wait & Watch', 'Avoid'",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of stocks to return", "default": 20},
                "verdict": {"type": "string", "description": "Filter by verdict (optional)"}
            }
        }
    },
    {
        "name": "get_sector",
        "description": "Get top GVM-scored stocks in a sector. Example segments: IT, Pharma, Banking, Auto, FMCG, Defence, Healthcare",
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment": {"type": "string", "description": "Sector or segment name"},
                "n": {"type": "integer", "description": "Number of stocks", "default": 20}
            },
            "required": ["segment"]
        }
    },
    {
        "name": "get_filter",
        "description": "Filter stocks by GVM score range from Railway DB",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_score": {"type": "number", "description": "Minimum GVM score", "default": 7.0},
                "max_score": {"type": "number", "description": "Maximum GVM score", "default": 10.0},
                "verdict": {"type": "string", "description": "Filter by verdict (optional)"},
                "n": {"type": "integer", "description": "Number of results", "default": 50}
            }
        }
    },
    {
        "name": "run_sql",
        "description": "Run any SQL query on Railway PostgreSQL. Use for schema checks, data queries, migrations, analytics. Never use for DROP TABLE, DELETE without WHERE, TRUNCATE.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SQL query to execute"},
                "params": {"type": "array", "description": "Query parameters (optional)", "default": []}
            },
            "required": ["query"]
        }
    },
    {
        "name": "load_input_from_drive",
        "description": "Reload input_raw table from a Google Drive file (must be Anyone-with-link-Viewer). Pass file_id from Drive URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Google Drive file ID"}
            },
            "required": ["file_id"]
        }
    },
    {
        "name": "load_screener_from_drive",
        "description": "Reload screener_raw table from a Google Drive file (must be Anyone-with-link-Viewer). Pass file_id from Drive URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Google Drive file ID"}
            },
            "required": ["file_id"]
        }
    },
    # DAY 32 NEW TOOLS
    {
        "name": "get_sector_rating",
        "description": "Get mcap-weighted GVM rating for sectors. Returns Strong/Buy/Watch/Avoid verdict, weighted G/V/M scores, top stock per sector. Pass segment name for one, omit for top sectors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment": {"type": "string", "description": "Optional sector name (partial match, e.g. 'Pharma', 'IT'). If omitted, returns top sectors."},
                "n": {"type": "integer", "description": "Number of sectors when listing all", "default": 20}
            }
        }
    },
    {
        "name": "get_momentum",
        "description": "Get momentum scores (1Y/3Y return, DMA50, DMA200, vs Nifty) and M score for a stock. Pass symbol for one, omit for top stocks by momentum.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "NSE symbol for single stock. Omit for top list."},
                "n": {"type": "integer", "description": "Number of stocks when listing top", "default": 20},
                "min_score": {"type": "number", "description": "Minimum M score filter for top list", "default": 7.0}
            }
        }
    },
    {
        "name": "health_feeds",
        "description": "Status dashboard for all data feeds — shows latest date and freshness verdict (fresh/ok/stale) for gvm_scores, raw_prices, screener_raw, input_raw, sector_ratings, momentum_scores.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

@app.get("/mcp")
async def mcp_sse(request: Request):
    async def event_stream():
        yield ": connected\n\n"
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )

@app.post("/mcp")
async def mcp_handler(request: Request):
    """MCP JSON-RPC 2.0 handler — streamable-http transport"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})

    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    if req_id is None and method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Scorr — Project Quant", "version": "1.1.0"}
                }
            },
            headers={"Mcp-Session-Id": str(uuid.uuid4())}
        )

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}

    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        try:
            result = await _call_tool(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
                }
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)}
            }

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


# ============================================
# REQUEST MODELS
# ============================================

class ParamRequest(BaseModel):
    stock_val: float
    peer_avg: float

class PromoterRequest(BaseModel):
    stock_val: float

class PERequest(BaseModel):
    pe: float
    historical_pe: float
    segment_pe: float

class DMARequest(BaseModel):
    price: float
    dma: float

class StockRequest(BaseModel):
    name: str
    price: float
    sales_growth_5y: float;    peer_sales_growth_5y: float
    sales_growth_3y: float;    peer_sales_growth_3y: float
    profit_growth_5y: float;   peer_profit_growth_5y: float
    profit_growth_3y: float;   peer_profit_growth_3y: float
    qoq_sales_growth: float;   peer_qoq_sales_growth: float
    qoq_profit_growth: float;  peer_qoq_profit_growth: float
    opm: float;                peer_opm: float
    opm_expansion: float;      peer_opm_expansion: float
    fixed_asset_growth: float; peer_fixed_asset_growth: float
    promoter_holding: float
    inst_holding_change: float; peer_inst_holding_change: float
    roce: float;                peer_roce: float
    interest_coverage: float;   peer_interest_coverage: float
    dividend_yield: float;      peer_dividend_yield: float
    pe: float; historical_pe: float; segment_pe: float
    potential_upside: float;    peer_potential_upside: float
    return_1y: float;           peer_return_1y: float
    return_3y: float;           peer_return_3y: float
    dma_50: float;  dma_200: float
    return_52w_vs_index: float; peer_return_52w_vs_index: float


# ============================================
# ROOT
# ============================================

@app.get("/")
def root():
    return {
        "message": "Project Quant — Trading API is live 🚀",
        "version": "1.1.0",
        "total_apis": 38,
        "mcp": "/mcp",
        "docs": "/docs"
    }


# ============================================
# GROWTH PARAMETER APIs (9)
# ============================================

@app.post("/api/growth/sales-growth-5y")
def sales_growth_5y(req: ParamRequest):
    return api_sales_growth_5y(req.stock_val, req.peer_avg)

@app.post("/api/growth/sales-growth-3y")
def sales_growth_3y(req: ParamRequest):
    return api_sales_growth_3y(req.stock_val, req.peer_avg)

@app.post("/api/growth/profit-growth-5y")
def profit_growth_5y(req: ParamRequest):
    return api_profit_growth_5y(req.stock_val, req.peer_avg)

@app.post("/api/growth/profit-growth-3y")
def profit_growth_3y(req: ParamRequest):
    return api_profit_growth_3y(req.stock_val, req.peer_avg)

@app.post("/api/growth/qoq-sales-growth")
def qoq_sales_growth(req: ParamRequest):
    return api_qoq_sales_growth(req.stock_val, req.peer_avg)

@app.post("/api/growth/qoq-profit-growth")
def qoq_profit_growth(req: ParamRequest):
    return api_qoq_profit_growth(req.stock_val, req.peer_avg)

@app.post("/api/growth/opm")
def opm(req: ParamRequest):
    return api_opm(req.stock_val, req.peer_avg)

@app.post("/api/growth/opm-expansion")
def opm_expansion(req: ParamRequest):
    return api_opm_expansion(req.stock_val, req.peer_avg)

@app.post("/api/growth/fixed-asset-growth")
def fixed_asset_growth(req: ParamRequest):
    return api_fixed_asset_growth(req.stock_val, req.peer_avg)


# ============================================
# RELIABILITY PARAMETER APIs (5)
# ============================================

@app.post("/api/reliability/promoter-holding")
def promoter_holding(req: PromoterRequest):
    return api_inst_holding_abs(req.stock_val)

@app.post("/api/reliability/inst-holding-change")
def inst_holding_change(req: ParamRequest):
    return api_inst_holding_change(req.stock_val, req.peer_avg)

@app.post("/api/reliability/roce")
def roce(req: ParamRequest):
    return api_roce(req.stock_val, req.peer_avg)

@app.post("/api/reliability/interest-coverage")
def interest_coverage(req: ParamRequest):
    return api_interest_coverage(req.stock_val, req.peer_avg)

@app.post("/api/reliability/dividend-yield")
def dividend_yield(req: ParamRequest):
    return api_dividend_yield(req.stock_val, req.peer_avg)


# ============================================
# VALUE PARAMETER APIs (2)
# ============================================

@app.post("/api/value/pe-ratio")
def pe_ratio(req: PERequest):
    return api_pe_ratio(req.pe, req.historical_pe, req.segment_pe)

@app.post("/api/value/potential-upside")
def potential_upside(req: ParamRequest):
    return api_potential_upside(req.stock_val, req.peer_avg)


# ============================================
# MOMENTUM PARAMETER APIs (5) — scoring functions
# ============================================

@app.post("/api/momentum/return-1y")
def return_1y(req: ParamRequest):
    return api_return_1y(req.stock_val, req.peer_avg)

@app.post("/api/momentum/return-3y")
def return_3y(req: ParamRequest):
    return api_return_3y(req.stock_val, req.peer_avg)

@app.post("/api/momentum/dma50")
def dma50(req: DMARequest):
    return api_dma50(req.price, req.dma)

@app.post("/api/momentum/dma200")
def dma200(req: DMARequest):
    return api_dma200(req.price, req.dma)

@app.post("/api/momentum/return-52w-vs-index")
def return_52w_vs_index(req: ParamRequest):
    return api_return_52w_vs_index(req.stock_val, req.peer_avg)


# ============================================
# COMPOSITE SCORE APIs (4)
# ============================================

@app.post("/api/score/g-score")
def g_score(req: StockRequest):
    return api_g_score(req.dict())

@app.post("/api/score/v-score")
def v_score(req: StockRequest):
    return api_v_score(req.dict())

@app.post("/api/score/m-score")
def m_score(req: StockRequest):
    return api_m_score(req.dict())

@app.post("/api/score/gvm-score")
def gvm_score(req: StockRequest):
    return api_gvm_score(req.dict())


# ============================================
# G11-B READ ENDPOINTS (GVM)
# ============================================

@app.get("/api/gvm/top")
def get_top_stocks(n: int = 20, verdict: str = None):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        latest = cur.fetchone()[0]
        if verdict:
            cur.execute("""
                SELECT symbol, company_name, segment, rank, price,
                       g_score, v_score, m_score, gvm_score, verdict
                FROM gvm_scores WHERE score_date=%s AND verdict=%s
                ORDER BY gvm_score DESC LIMIT %s
            """, (latest, verdict, n))
        else:
            cur.execute("""
                SELECT symbol, company_name, segment, rank, price,
                       g_score, v_score, m_score, gvm_score, verdict
                FROM gvm_scores WHERE score_date=%s
                ORDER BY gvm_score DESC LIMIT %s
            """, (latest, n))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {"date": str(latest), "count": len(rows), "stocks": [
            {"symbol": r[0], "company_name": r[1], "segment": r[2],
             "rank": r[3], "price": float(r[4] or 0),
             "g_score": float(r[5] or 0), "v_score": float(r[6] or 0),
             "m_score": float(r[7] or 0), "gvm_score": float(r[8] or 0),
             "verdict": r[9]} for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gvm/filter")
def filter_stocks(min_score: float = 0, max_score: float = 10, verdict: str = None, n: int = 50):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        latest = cur.fetchone()[0]
        if verdict:
            cur.execute("""
                SELECT symbol, company_name, segment, rank, price,
                       g_score, v_score, m_score, gvm_score, verdict
                FROM gvm_scores WHERE score_date=%s AND verdict=%s
                  AND gvm_score BETWEEN %s AND %s
                ORDER BY gvm_score DESC LIMIT %s
            """, (latest, verdict, min_score, max_score, n))
        else:
            cur.execute("""
                SELECT symbol, company_name, segment, rank, price,
                       g_score, v_score, m_score, gvm_score, verdict
                FROM gvm_scores WHERE score_date=%s
                  AND gvm_score BETWEEN %s AND %s
                ORDER BY gvm_score DESC LIMIT %s
            """, (latest, min_score, max_score, n))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {"date": str(latest), "count": len(rows), "stocks": [
            {"symbol": r[0], "company_name": r[1], "segment": r[2],
             "rank": r[3], "price": float(r[4] or 0),
             "g_score": float(r[5] or 0), "v_score": float(r[6] or 0),
             "m_score": float(r[7] or 0), "gvm_score": float(r[8] or 0),
             "verdict": r[9]} for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gvm/sector")
def get_by_sector(segment: str, n: int = 20):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        latest = cur.fetchone()[0]
        cur.execute("""
            SELECT symbol, company_name, segment, rank, price,
                   g_score, v_score, m_score, gvm_score, verdict
            FROM gvm_scores WHERE score_date=%s AND segment ILIKE %s
            ORDER BY gvm_score DESC LIMIT %s
        """, (latest, f"%{segment}%", n))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {"segment": segment, "date": str(latest), "count": len(rows), "stocks": [
            {"symbol": r[0], "company_name": r[1], "segment": r[2],
             "rank": r[3], "price": float(r[4] or 0),
             "g_score": float(r[5] or 0), "v_score": float(r[6] or 0),
             "m_score": float(r[7] or 0), "gvm_score": float(r[8] or 0),
             "verdict": r[9]} for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gvm/{symbol}")
def get_gvm_by_symbol(symbol: str):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, company_name, segment, rank, price,
                   g_score, v_score, m_score, gvm_score,
                   growth_label, value_label, momentum_label,
                   gvm_overall_label, verdict, punchline, score_date
            FROM gvm_scores WHERE symbol=%s
            ORDER BY score_date DESC LIMIT 1
        """, (symbol.upper(),))
        r = cur.fetchone()
        cur.close(); conn.close()
        if not r:
            raise HTTPException(status_code=404, detail=f"{symbol} not found")
        return {
            "symbol": r[0], "company_name": r[1], "segment": r[2],
            "rank": r[3], "price": float(r[4] or 0),
            "g_score": float(r[5] or 0), "v_score": float(r[6] or 0),
            "m_score": float(r[7] or 0), "gvm_score": float(r[8] or 0),
            "growth_label": r[9], "value_label": r[10], "momentum_label": r[11],
            "gvm_overall_label": r[12], "verdict": r[13],
            "punchline": r[14], "score_date": str(r[15])
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# DAY 32 — SECTOR RATING ENDPOINTS
# ============================================

@app.get("/api/sector/rating")
def sector_rating_all(n: int = 200):
    """All sectors ranked by mcap-weighted GVM, with verdicts."""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT segment, stocks_count,
                   CAST(mcap_weighted_gvm AS FLOAT),
                   CAST(weighted_g AS FLOAT), CAST(weighted_v AS FLOAT), CAST(weighted_m AS FLOAT),
                   CAST(simple_avg_gvm AS FLOAT), CAST(total_mcap AS FLOAT),
                   top_stock, CAST(top_stock_gvm AS FLOAT), verdict,
                   score_date::TEXT
            FROM sector_ratings
            WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)
            ORDER BY mcap_weighted_gvm DESC
            LIMIT %s
        """, (n,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {"count": len(rows), "sectors": [
            {"segment": r[0], "stocks": r[1], "gvm": r[2],
             "g": r[3], "v": r[4], "m": r[5],
             "avg_gvm": r[6], "total_mcap": r[7],
             "top_stock": r[8], "top_gvm": r[9],
             "verdict": r[10], "score_date": r[11]} for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sector/rating/{segment}")
def sector_rating_one(segment: str):
    """Rating for one specific sector (partial match)."""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT segment, stocks_count,
                   CAST(mcap_weighted_gvm AS FLOAT),
                   CAST(weighted_g AS FLOAT), CAST(weighted_v AS FLOAT), CAST(weighted_m AS FLOAT),
                   CAST(simple_avg_gvm AS FLOAT), CAST(total_mcap AS FLOAT),
                   top_stock, CAST(top_stock_gvm AS FLOAT), verdict,
                   score_date::TEXT
            FROM sector_ratings
            WHERE segment ILIKE %s
            AND score_date = (SELECT MAX(score_date) FROM sector_ratings)
        """, (f"%{segment}%",))
        r = cur.fetchone()
        cur.close(); conn.close()
        if not r:
            raise HTTPException(status_code=404, detail=f"Sector '{segment}' not found")
        return {"segment": r[0], "stocks": r[1], "gvm": r[2],
                "g": r[3], "v": r[4], "m": r[5],
                "avg_gvm": r[6], "total_mcap": r[7],
                "top_stock": r[8], "top_gvm": r[9],
                "verdict": r[10], "score_date": r[11]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# DAY 32 — MOMENTUM DATA ENDPOINTS
# Note: Using /api/momentum/stock/{symbol} to avoid conflict with
# existing POST /api/momentum/return-1y, dma50, dma200 scoring endpoints.
# ============================================

@app.get("/api/momentum/top")
def momentum_top(n: int = 20, min_score: float = 7.0):
    """Top stocks by M score, joined with GVM data."""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT m.symbol,
                   CAST(m.latest_price AS FLOAT),
                   CAST(m.ret_1y AS FLOAT), CAST(m.ret_3y AS FLOAT),
                   CAST(m.dma_50 AS FLOAT), CAST(m.dma_200 AS FLOAT),
                   CAST(m.ret_52w_vs_index AS FLOAT),
                   CAST(m.m_score AS FLOAT),
                   g.segment,
                   CAST(g.gvm_score AS FLOAT), g.verdict
            FROM momentum_scores m
            LEFT JOIN gvm_scores g ON g.symbol = m.symbol
                AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
            WHERE m.score_date = (SELECT MAX(score_date) FROM momentum_scores)
            AND m.m_score >= %s
            ORDER BY m.m_score DESC, m.ret_1y DESC
            LIMIT %s
        """, (min_score, n))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {"count": len(rows), "stocks": [
            {"symbol": r[0], "price": r[1],
             "ret_1y": r[2], "ret_3y": r[3],
             "dma_50": r[4], "dma_200": r[5],
             "vs_nifty": r[6], "m_score": r[7],
             "segment": r[8], "gvm_score": r[9], "verdict": r[10]} for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/momentum/stock/{symbol}")
def momentum_for_symbol(symbol: str):
    """Momentum scores for one stock — all 5 components + ratings + M score."""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol,
                   CAST(latest_price AS FLOAT),
                   CAST(ret_1y AS FLOAT), CAST(ret_3y AS FLOAT),
                   CAST(dma_50 AS FLOAT), CAST(dma_200 AS FLOAT),
                   CAST(ret_52w_vs_index AS FLOAT),
                   CAST(ret_1y_rating AS FLOAT), CAST(ret_3y_rating AS FLOAT),
                   CAST(dma_50_rating AS FLOAT), CAST(dma_200_rating AS FLOAT),
                   CAST(ret_52w_idx_rating AS FLOAT),
                   CAST(m_score AS FLOAT),
                   score_date::TEXT
            FROM momentum_scores
            WHERE symbol = %s
            AND score_date = (SELECT MAX(score_date) FROM momentum_scores)
        """, (symbol.upper(),))
        r = cur.fetchone()
        cur.close(); conn.close()
        if not r:
            raise HTTPException(status_code=404, detail=f"{symbol} not in momentum_scores")
        return {"symbol": r[0], "price": r[1],
                "ret_1y": r[2], "ret_3y": r[3],
                "dma_50": r[4], "dma_200": r[5],
                "vs_nifty": r[6],
                "rating_1y": r[7], "rating_3y": r[8],
                "rating_dma50": r[9], "rating_dma200": r[10],
                "rating_vs_idx": r[11],
                "m_score": r[12], "score_date": r[13]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# DAY 32 — HEALTH / FEED STATUS
# ============================================

@app.get("/api/health/feeds")
def health_feeds():
    """Status of all data feeds — latest date and freshness verdict."""
    try:
        today = date.today()
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT 'gvm_scores' AS source, MAX(score_date)::TEXT, COUNT(*) FROM gvm_scores
            UNION ALL
            SELECT 'raw_prices', MAX(price_date)::TEXT, COUNT(DISTINCT symbol) FROM raw_prices
            UNION ALL
            SELECT 'screener_raw', NULL, COUNT(*) FROM screener_raw
            UNION ALL
            SELECT 'input_raw', NULL, COUNT(*) FROM input_raw
            UNION ALL
            SELECT 'sector_ratings', MAX(score_date)::TEXT, COUNT(*) FROM sector_ratings
            UNION ALL
            SELECT 'momentum_scores', MAX(score_date)::TEXT, COUNT(*) FROM momentum_scores
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()

        feeds = []
        for r in rows:
            source, latest, records = r[0], r[1], r[2]
            if latest:
                latest_dt = datetime.strptime(latest, "%Y-%m-%d").date()
                days = (today - latest_dt).days
                if days <= 1: freshness = "fresh"
                elif days <= 3: freshness = "ok"
                elif days <= 7: freshness = "stale"
                else: freshness = "very_stale"
            else:
                freshness = "n/a"
                days = None
            feeds.append({
                "source": source, "latest": latest, "records": records,
                "freshness": freshness, "days_old": days
            })
        return {"checked_at": today.isoformat(), "feeds": feeds}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# STARTUP + SCHEDULER
# ============================================

@app.on_event("startup")
def startup_event():
    create_tables()


def run_daily_update():
    """Yahoo OHLC update + auto-refresh momentum + sector ratings."""
    logging.info("[SCHEDULER] Daily OHLC update started")
    try:
        result = subprocess.run(
            ["python", "yahoo_daily_update.py"],
            capture_output=True, text=True, timeout=1800
        )
        logging.info(f"[SCHEDULER] Yahoo update done: {result.stdout[-300:]}")
    except Exception as e:
        logging.error(f"[SCHEDULER] Yahoo failed: {e}")

    # DAY 32 — auto-refresh momentum + sector_ratings after price update
    try:
        with httpx.Client(timeout=300) as client:
            r1 = client.post(f"{BASE_URL}/api/admin/refresh_momentum")
            logging.info(f"[SCHEDULER] Momentum refresh: {r1.json()}")
            r2 = client.post(f"{BASE_URL}/api/admin/refresh_sector_ratings")
            logging.info(f"[SCHEDULER] Sector refresh: {r2.json()}")
    except Exception as e:
        logging.error(f"[SCHEDULER] Auto-refresh failed: {e}")


scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    run_daily_update,
    CronTrigger(hour=10, minute=15, day_of_week="mon-fri")
)
scheduler.start()
logging.basicConfig(level=logging.INFO)