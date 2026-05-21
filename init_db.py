import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gvm_scores (
            id BIGSERIAL PRIMARY KEY,
            nse_code VARCHAR(20) NOT NULL,
            stock_name VARCHAR(100),
            segment VARCHAR(50),
            g_score NUMERIC(4,2),
            v_score NUMERIC(4,2),
            m_score NUMERIC(4,2),
            gvm_score NUMERIC(4,2),
            verdict VARCHAR(20),
            commentary TEXT,
            score_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw_prices (
            id BIGSERIAL PRIMARY KEY,
            nse_code VARCHAR(20) NOT NULL,
            price_date DATE NOT NULL,
            open NUMERIC(10,2),
            high NUMERIC(10,2),
            low NUMERIC(10,2),
            close NUMERIC(10,2),
            volume BIGINT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(nse_code, price_date)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id BIGSERIAL PRIMARY KEY,
            nse_code VARCHAR(20) NOT NULL,
            signal_date DATE NOT NULL,
            signal_type VARCHAR(10),
            strategy VARCHAR(20),
            entry_price NUMERIC(10,2),
            target_price NUMERIC(10,2),
            stop_loss NUMERIC(10,2),
            status VARCHAR(20) DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            email VARCHAR(100) UNIQUE NOT NULL,
            name VARCHAR(100),
            plan VARCHAR(20) DEFAULT 'free',
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS intraday_1min (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            timestamp TIMESTAMP NOT NULL,
            close NUMERIC(10,2),
            volume BIGINT,
            inserted_at TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_intraday_symbol_time 
            ON intraday_1min (symbol, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_intraday_timestamp 
            ON intraday_1min (timestamp DESC);
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("✅ All 5 tables created successfully.")

if __name__ == "__main__":
    init_db()