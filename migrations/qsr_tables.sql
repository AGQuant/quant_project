-- cc#1175 · QSR ENGINE V1 tables (session_log 27980, founder-locked 20-Aug-2026)
--
-- CREATE TABLE IF NOT EXISTS only. No ALTER on anything that already exists, per the card and
-- MAINTENANCE_LOCK_RULE. Every table is prefixed qsr_ and references no v8_/tc_/v14_ table, so
-- context isolation (rule 7) holds by construction rather than by care.
--
-- THE FUNNEL TABLE IS NOT OPTIONAL AND IT IS NOT AN AFTERTHOUGHT. 27980 calls it a
-- non-negotiable and names the reason: V14 shipped without one and nobody could tell a day when
-- nothing qualified from a day when the engine did not run. qsr_funnel_daily is written on EVERY
-- scan, including — especially — scans that qualify nobody.

CREATE TABLE IF NOT EXISTS qsr_funnel_daily (
    id            SERIAL PRIMARY KEY,
    scan_date     DATE        NOT NULL,
    scan_ts       TIMESTAMP   NOT NULL,     -- naive IST, the convention every engine here uses
    universe      INTEGER     NOT NULL,
    -- stage survivor counts, in gate order
    s_quality     INTEGER     NOT NULL,     -- GVM > 7 AND dGVM_90d >= +0.5
    s_sector      INTEGER     NOT NULL,     -- segment week > NIFTY week * 1.5 AND segment week > 0
    s_returns     INTEGER     NOT NULL,     -- day 0-3, week 0-5, month 0-10
    s_location    INTEGER     NOT NULL,     -- CMP >= PP AND low touched S1 in last 3 sessions
    s_volume      INTEGER     NOT NULL,     -- 2-of-3 soft gate
    qualified     INTEGER     NOT NULL,     -- cleared everything
    entered       INTEGER     NOT NULL,     -- actually opened after slot/rank caps
    -- WHY each stage killed what it killed. A count alone tells you a day was empty; this tells
    -- you which gate emptied it, which is the whole point of writing a ledger on a zero day.
    fails         JSONB,                    -- {stage: [symbol, ...]} capped per stage
    notes         TEXT,
    CONSTRAINT qsr_funnel_daily_scan_date_key UNIQUE (scan_date)
);

CREATE TABLE IF NOT EXISTS qsr_positions (
    id            SERIAL PRIMARY KEY,
    symbol        TEXT        NOT NULL,
    entry_date    DATE        NOT NULL,
    entry_ts      TIMESTAMP   NOT NULL,     -- naive IST
    entry_price   NUMERIC     NOT NULL,
    qty           INTEGER     NOT NULL,     -- 1L notional / entry_price, floored
    notional      NUMERIC     NOT NULL,
    stop_price    NUMERIC     NOT NULL,     -- -8% hard stop, frozen at entry
    gvm_at_entry  NUMERIC,
    dgvm_90d      NUMERIC,                  -- the rank key for the 3-per-day cap
    segment       TEXT,
    gates         JSONB,                    -- every gate's value at entry, for the audit trail
    status        TEXT        NOT NULL DEFAULT 'OPEN',
    CONSTRAINT qsr_positions_open_once UNIQUE (symbol, entry_date)
);

CREATE TABLE IF NOT EXISTS qsr_trades (
    id            SERIAL PRIMARY KEY,
    symbol        TEXT        NOT NULL,
    entry_date    DATE        NOT NULL,
    entry_ts      TIMESTAMP   NOT NULL,
    entry_price   NUMERIC     NOT NULL,
    qty           INTEGER     NOT NULL,
    exit_date     DATE,
    exit_ts       TIMESTAMP,
    exit_price    NUMERIC,
    -- HARD_STOP | QUALITY_BREAK | TIME_STOP. There is deliberately no TARGET: 27980 says winners
    -- run until an exit fires, so a target value would be a rule nobody wrote.
    exit_reason   TEXT,
    held_sessions INTEGER,
    pnl           NUMERIC,
    pnl_pct       NUMERIC,
    gvm_at_entry  NUMERIC,
    gvm_at_exit   NUMERIC,
    dgvm_90d      NUMERIC,
    segment       TEXT,
    gates         JSONB
);

CREATE INDEX IF NOT EXISTS qsr_positions_status_idx ON qsr_positions (status);
CREATE INDEX IF NOT EXISTS qsr_trades_exit_date_idx ON qsr_trades (exit_date DESC);
CREATE INDEX IF NOT EXISTS qsr_funnel_daily_date_idx ON qsr_funnel_daily (scan_date DESC);
