import psycopg2
import os

DATABASE_URL = "postgresql://postgres:ZKseuvlpIatqRCSZTiUUOyJWBVUiLueA@kodama.proxy.rlwy.net:20061/railway"

conn = psycopg2.connect(DATABASE_URL, sslmode='disable')
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS gvm_scores (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    company_name VARCHAR(200),
    segment VARCHAR(100),
    gvm_score DECIMAL(5,2),
    growth_score DECIMAL(5,2),
    value_score DECIMAL(5,2),
    momentum_score DECIMAL(5,2),
    verdict VARCHAR(50),
    score_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS raw_prices (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    price_date DATE NOT NULL,
    open DECIMAL(12,2),
    high DECIMAL(12,2),
    low DECIMAL(12,2),
    close DECIMAL(12,2),
    volume BIGINT,
    UNIQUE(symbol, price_date)
);

CREATE TABLE IF NOT EXISTS signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    signal_type VARCHAR(10),
    signal_date DATE NOT NULL,
    entry_price DECIMAL(12,2),
    target DECIMAL(12,2),
    stop_loss DECIMAL(12,2),
    strategy VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(200) UNIQUE NOT NULL,
    name VARCHAR(200),
    plan VARCHAR(50) DEFAULT 'free',
    created_at TIMESTAMP DEFAULT NOW()
);
""")

conn.commit()
cur.close()
conn.close()
print("All 4 tables created successfully.")