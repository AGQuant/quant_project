-- cc#1174 push 1 — INVESTMENT_CHECK_V2 component-weight registry.
-- WEIGHTS ARE THE FOUNDER LOCK, session_log 27979 (INVESTMENT_CHECK_V2_LOCKED, 20-Aug-2026 ~23:05
-- IST). Nine components, totalling 100. Every row below traces to that entry and nothing else.
--
-- WHY A REGISTRY AND NOT CONSTANTS — same reason as tc_rule_weights (cc#1172). The founder
-- re-weights after chart review; weights in Python mean a push per calibration round, weights in a
-- row mean an UPDATE. The engine reads this table at compute time, so a re-weight is data, not a
-- deploy.
--
-- SEPARATE TABLE FROM tc_rule_weights ON PURPOSE. Context isolation (session_log id 244, carried by
-- 324 and restated in 27979): Investment Check shares the SCORE GRAMMAR with Trade Check and shares
-- nothing else — not rules, not data paths, not a weight table. One shared registry would be the
-- first quiet step toward one shared rulebook.
--
-- CREATE TABLE ONLY — never ALTER an existing table (MAINTENANCE_LOCK_RULE, cc#351). Idempotent.
-- ON CONFLICT DO NOTHING is deliberate: this file SEEDS a fresh database and must never overwrite a
-- calibration the founder made against the registry after it was written.
--
-- ON THE DENOMINATOR. The score divides by the sum of weights over COMPUTABLE components only, so a
-- symbol whose gvm_history is too shallow for dGVM is scored out of 90, not penalised out of 100.
-- The payload carries that denominator with every score. A component that cannot be computed is
-- excluded and said so; it is never a silent zero.

CREATE TABLE IF NOT EXISTS ic_rule_weights (
    bucket      TEXT    NOT NULL,          -- 'invest' (one bucket today; the column keeps room)
    rule_key    TEXT    NOT NULL,          -- engine component key
    rule_label  TEXT,                      -- the readable label the report renders
    weight      NUMERIC NOT NULL DEFAULT 1,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    source      TEXT,                      -- the founder-lock session_log id
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (bucket, rule_key)
);

-- ── the 9 locked components · weights sum to 100 ────────────────────────────────────────────
INSERT INTO ic_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('invest','gvm_level',      'GVM level (graded)',              40, '27979'),
    ('invest','fundamentals',   'Fundamentals cluster (5 checks)', 12, '27979'),
    ('invest','dgvm_90d',       'GVM direction, 90 days',          10, '27979'),
    ('invest','dma_stack',      'DMA stack 20>50>200',              8, '27979'),
    ('invest','accumulation',   'Accumulation, 21 sessions',        8, '27979'),
    ('invest','s1_reclaim',     'S1 touch + reclaim',               8, '27979'),
    ('invest','rs_1y',          'RS 1 year vs NIFTY50',             8, '27979'),
    ('invest','segment_month',  'Segment month',                    4, '27979'),
    ('invest','rsi_monthly',    'Monthly RSI guard (35-75)',        2, '27979')
ON CONFLICT (bucket, rule_key) DO NOTHING;
