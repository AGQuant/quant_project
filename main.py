from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
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


def create_tables():
    try:
        import psycopg
        import urllib.parse
        db_url = os.environ.get("DATABASE_URL", "")
        parsed = urllib.parse.urlparse(db_url)
        conn = psycopg.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            dbname=parsed.path.lstrip('/'),
            user=parsed.username,
            password=parsed.password
        )
        cursor = conn.cursor()
        
        # GVM_SCORES — Full 80-column audit schema (DECIMAL(15,2) for raw/peer to prevent overflow)
        cursor.execute("""DROP TABLE IF EXISTS gvm_scores CASCADE""")
        cursor.execute("""CREATE TABLE gvm_scores (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            company_name VARCHAR(200),
            segment VARCHAR(100),
            rank INT,
            price DECIMAL(12,2),
            
            g_score DECIMAL(5,2),
            v_score DECIMAL(5,2),
            m_score DECIMAL(5,2),
            gvm_score DECIMAL(5,2),
            growth_label VARCHAR(50),
            value_label VARCHAR(50),
            momentum_label VARCHAR(50),
            gvm_overall_label VARCHAR(50),
            verdict VARCHAR(50),
            punchline TEXT,
            
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
            id SERIAL PRIMARY KEY, symbol VARCHAR(20) NOT NULL,
            price_date DATE NOT NULL, open DECIMAL(12,2),
            high DECIMAL(12,2), low DECIMAL(12,2),
            close DECIMAL(12,2), volume BIGINT,
            UNIQUE(symbol, price_date))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY, symbol VARCHAR(20),
            signal_type VARCHAR(50), created_at TIMESTAMP DEFAULT NOW())""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, email VARCHAR(200) UNIQUE,
            created_at TIMESTAMP DEFAULT NOW())""")
        conn.commit()
        cursor.close()
        conn.close()
        print("Tables created successfully")
    except Exception as e:
        print(f"Table creation error: {e}")


app = FastAPI(
    title="Project Quant — Trading API",
    description="Proprietary GVM quant scoring engine — 25 individual parameter APIs",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        "total_apis": 25,
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


@app.on_event("startup")
def startup_event():
    create_tables()