-- cc#1013 Portfolio Health: portfolio-level INVESTED AMOUNT + CASH + TRACKING DATE.
-- Lets the /health report + /adaptive dashboard show an honest headline P&L when per-holding buy
-- prices are unknown (all hr_holdings.avg_price NULL -> the SUM(qty*avg_price) cost basis is 0/garbage,
-- which is how Vishal Bhosale's card showed +1880.5%). CREATE TABLE only -- never ALTER an existing
-- table (MAINTENANCE_LOCK rule). Idempotent; mirrored in main.py's startup DDL for fresh DBs.
--
-- FOUNDER RULE (locked 11-Aug-2026): when a meta invested_amount is set it IS the headline Invested,
-- and headline P&L = current_value + cash - invested_amount (% on invested). Holdings cost
-- (SUM qty*avg_price) is demoted to a secondary "Holdings cost / Unrealised" line.

CREATE TABLE IF NOT EXISTS hr_portfolio_meta (
    portfolio_id    INTEGER PRIMARY KEY REFERENCES hr_portfolios(id) ON DELETE CASCADE,
    invested_amount NUMERIC,
    cash            NUMERIC DEFAULT 0,
    tracking_date   DATE,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Founder register 11-Aug-2026 (per-client invested amount + tracking date).
INSERT INTO hr_portfolio_meta (portfolio_id, invested_amount, cash, tracking_date) VALUES
    (4,   2150067, 0, '2026-02-03'),   -- Vishal Bhosale (26 holdings, all avg_price NULL)
    (165, 2768000, 0, '2025-05-15'),   -- Akshay Gupta   (29 holdings, all avg_price NULL)
    (180, 865162,  0, '2025-09-10')    -- Ritesh         (25 holdings, full prices; header now on invested basis)
ON CONFLICT (portfolio_id) DO UPDATE
    SET invested_amount = EXCLUDED.invested_amount,
        cash            = EXCLUDED.cash,
        tracking_date   = EXCLUDED.tracking_date,
        updated_at      = NOW();
