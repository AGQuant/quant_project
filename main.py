from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import os
import psycopg
import urllib.parse
import secrets
import logging
import subprocess
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


# redirect_slashes=False fixes POST /mcp → 307 redirect issue
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
# Auto-approve flow (private server)
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
    return {
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL]
    }

@app.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource_mcp():
    return {
        "resource": f"{BASE_URL}/mcp",
        "authorization_servers": [BASE_URL]
    }

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
# MCP SERVER — Mount at /mcp
# ============================================

from mcp_server import mcp
app.mount("/mcp", mcp.streamable_http_app())


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
# MUST define /top, /filter, /sector before /{symbol}
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