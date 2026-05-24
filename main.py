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
        conn.commit()
        cursor.close()
        conn.close()
        print("Tables ready")
    except Exception as e:
        print(f"Table creation error: {e}")


app = FastAPI(
    title="Project Quant — Trading API",
    description="Proprietary GVM quant scoring engine — 29 APIs + MCP",
    version="1.0.0",
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
                    "serverInfo": {"name": "Scorr — Project Quant", "version": "1.0.0"}
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
        "version": "1.0.0",
        "total_apis": 29,
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
# MOMENTUM PARAMETER APIs (5)
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
# G11-B READ ENDPOINTS
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
# STARTUP + SCHEDULER
# ============================================

@app.on_event("startup")
def startup_event():
    create_tables()


def run_daily_update():
    logging.info("[SCHEDULER] Daily OHLC update started")
    try:
        result = subprocess.run(
            ["python", "yahoo_daily_update.py"],
            capture_output=True, text=True, timeout=1800
        )
        logging.info(f"[SCHEDULER] Done: {result.stdout[-300:]}")
    except Exception as e:
        logging.error(f"[SCHEDULER] Failed: {e}")


scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    run_daily_update,
    CronTrigger(hour=10, minute=15, day_of_week="mon-fri")
)
scheduler.start()
logging.basicConfig(level=logging.INFO)