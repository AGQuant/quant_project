-- cc#1273: CLIENT_BASKET_REBALANCE_MULTIPLIER_V1
-- Two new tables for per-client basket subscriptions and repair audit log

CREATE TABLE IF NOT EXISTS client_basket_subscription (
    id SERIAL PRIMARY KEY,
    portfolio_id INTEGER NOT NULL REFERENCES hr_portfolios(id),
    basket_name TEXT NOT NULL REFERENCES quant_basket_registry(basket_name),
    multiplier INTEGER NOT NULL CHECK (multiplier >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (portfolio_id, basket_name) WHERE status = 'active'
);

CREATE TABLE IF NOT EXISTS client_basket_repair_log (
    id SERIAL PRIMARY KEY,
    portfolio_id INTEGER NOT NULL,
    basket_name TEXT NOT NULL,
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    symbol TEXT NOT NULL,
    target_qty INTEGER NOT NULL,
    actual_qty INTEGER NOT NULL,
    diff_qty INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD')),
    multiplier_used INTEGER NOT NULL
);

-- Index on (portfolio_id, basket_name) for fast subscription lookups
CREATE INDEX IF NOT EXISTS idx_client_basket_sub_portfolio_basket
ON client_basket_subscription(portfolio_id, basket_name);

-- Index on (portfolio_id) for available baskets query
CREATE INDEX IF NOT EXISTS idx_client_basket_sub_portfolio
ON client_basket_subscription(portfolio_id);

-- Index on (portfolio_id, basket_name, computed_at) for repair audit queries
CREATE INDEX IF NOT EXISTS idx_client_basket_repair_log_portfolio_basket
ON client_basket_repair_log(portfolio_id, basket_name, computed_at DESC);
