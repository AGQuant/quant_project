-- cc#1172 push 1/8 — TC_SCORE_V1 rule-weight registry (session_log 27957).
--
-- WHY A REGISTRY AND NOT CONSTANTS. The founder re-weights from tomorrow's chart review, and after
-- three trading days a sweet-spot search re-weights again. Weights that live in Python mean a push
-- per calibration round; weights that live in a row mean an UPDATE. The engine reads this table at
-- compute time, so a re-weight is data, not a deploy.
--
-- CREATE TABLE ONLY — never ALTER an existing table (MAINTENANCE_LOCK_RULE, cc#351). Idempotent.
--
-- HOW THE MAPPING WAS BUILT. Not from memory: tc_v4_dual._rules({}, style, side) emits EVERY rule
-- for a bucket regardless of data (cc#935), so running it over an empty symbol enumerates the live
-- rulebook exactly. That enumeration — 59 rule instances across the four buckets — was diffed
-- against the four weight tables in session_log 27957 by rule id. Every row below is a live rule.
--
-- THE GATE, ANSWERED HONESTLY (spec push 1 requires this and forbids a silent default).
-- SELL-MOM and SELL-REV map 1:1 — 12 and 11 live rules, 12 and 11 weights, no leftovers either way.
-- The BUY buckets do not. NINE rule instances are live but unnamed in 27957. Per the spec they are
-- seeded at weight 1 and are listed by name here and in the task log for Fable's ruling:
--     BUY-MOM   R16 Fib position (3M) · R20 GVM trend (d180d) · R21 dma_50 band · R23 GVM floor
--     BUY-REV   R4 MAs graded · R8 Returns mo · R16 Fib position (3M) · R20 GVM trend (d180d)
--               · R23 GVM floor
-- 27957's BUY-REV table carries a catch-all "remaining structure rules: 1". R4/R8/R16 plausibly sit
-- under it; R20 and R23 are GVM inputs, not structure, so reading them into it would be a guess.
-- All five are seeded at 1 and flagged rather than assumed either way.
--
-- NOTE ON WEIGHT vs TODAY'S max. These are independent. `weight` is the registry's contribution to
-- the normalized 0-10 score; the rule's `max` in tc_v4_dual is what it can score today. R5 Volume
-- is max 1.0 on BUY-MOM but max 2.0 on the other three; 27957 gives it weight 3 everywhere. The
-- rulebook is NOT edited by this card — pass/fail logic and halves stay byte-identical.

CREATE TABLE IF NOT EXISTS tc_rule_weights (
    bucket      TEXT    NOT NULL,          -- BUY-MOM | BUY-REV | SELL-MOM | SELL-REV
    rule_key    TEXT    NOT NULL,          -- tc_v4_dual legacy rule id (R1, R5, R19 ...)
    rule_label  TEXT,                      -- the live label, for a readable registry
    weight      NUMERIC NOT NULL DEFAULT 1,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    source      TEXT,                      -- '27957' where named, 'unmapped_seed_1' where not
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (bucket, rule_key)
);

-- ── BUY-MOM · 19 live rules ─────────────────────────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('BUY-MOM','R19','Relative strength (63d)',        3,'27957'),
    ('BUY-MOM','R5', 'Volume confirm',                 3,'27957'),
    ('BUY-MOM','R4', 'MAs 2of3',                       2,'27957'),
    ('BUY-MOM','R7', 'RSI frame',                      2,'27957'),
    ('BUY-MOM','R10','Style extra',                    2,'27957'),
    ('BUY-MOM','R18','Momentum (stock+sector M)',      2,'27957'),
    ('BUY-MOM','R11','Location + room',                2,'27957'),
    ('BUY-MOM','R1', 'Mood (fails)',                   1,'27957'),
    ('BUY-MOM','R2', 'Sector wk&mo',                   1,'27957'),
    ('BUY-MOM','R3', 'Peers',                          1,'27957'),
    ('BUY-MOM','R9', '5m + VWAP',                      1,'27957'),
    ('BUY-MOM','R12','Derivatives confirm',            1,'27957'),
    ('BUY-MOM','R17','Valuation (V)',                  1,'27957'),
    ('BUY-MOM','R22','52w index',                      1,'27957'),
    ('BUY-MOM','R24','Delivery confirm',               1,'27957'),
    ('BUY-MOM','R16','Fib position (3M)',              1,'unmapped_seed_1'),
    ('BUY-MOM','R20','GVM trend (d180d)',              1,'unmapped_seed_1'),
    ('BUY-MOM','R21','dma_50 band',                    1,'unmapped_seed_1'),
    ('BUY-MOM','R23','GVM floor',                      1,'unmapped_seed_1')
ON CONFLICT (bucket, rule_key) DO NOTHING;

-- ── BUY-REV · 17 live rules ─────────────────────────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('BUY-REV','R19','Relative strength (63d)',        3,'27957'),
    ('BUY-REV','R5', 'Volume confirm',                 3,'27957'),
    ('BUY-REV','R10','Style extra (mom_2d turn)',      2,'27957'),
    ('BUY-REV','R17','Valuation (V)',                  2,'27957'),
    ('BUY-REV','R18','Momentum (stock+sector M)',      2,'27957'),
    ('BUY-REV','R11','Location + room',                2,'27957'),
    ('BUY-REV','R7', 'RSI monthly',                    2,'27957'),
    ('BUY-REV','R1', 'Mood (fails)',                   1,'27957'),
    ('BUY-REV','R2', 'Sector mo',                      1,'27957'),
    ('BUY-REV','R3', 'Peers',                          1,'27957'),
    ('BUY-REV','R9', '5m + VWAP',                      1,'27957'),
    ('BUY-REV','R12','Derivatives confirm',            1,'27957'),
    ('BUY-REV','R4', 'MAs graded',                     1,'unmapped_seed_1'),
    ('BUY-REV','R8', 'Returns mo',                     1,'unmapped_seed_1'),
    ('BUY-REV','R16','Fib position (3M)',              1,'unmapped_seed_1'),
    ('BUY-REV','R20','GVM trend (d180d)',              1,'unmapped_seed_1'),
    ('BUY-REV','R23','GVM floor',                      1,'unmapped_seed_1')
ON CONFLICT (bucket, rule_key) DO NOTHING;

-- ── SELL-MOM · 12 live rules · maps 1:1 to 27957 ────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('SELL-MOM','R19','Relative strength (63d)',       3,'27957'),
    ('SELL-MOM','R5', 'Volume confirm',                3,'27957'),
    ('SELL-MOM','R4', 'MAs 2of3',                      2,'27957'),
    ('SELL-MOM','R7', 'RSI weekly cold',               2,'27957'),
    ('SELL-MOM','R10','2-day fall band',               2,'27957'),
    ('SELL-MOM','R18','Momentum decay (dM 180d)',      2,'27957'),
    ('SELL-MOM','R11','Below PP + room to S1',         2,'27957'),
    ('SELL-MOM','R1', 'Mood (any bearish)',            1,'27957'),
    ('SELL-MOM','R2', 'Sector mo',                     1,'27957'),
    ('SELL-MOM','R3', 'Peers',                         1,'27957'),
    ('SELL-MOM','R9', '5m + VWAP',                     1,'27957'),
    ('SELL-MOM','R12','Derivatives confirm',           1,'27957')
ON CONFLICT (bucket, rule_key) DO NOTHING;

-- ── SELL-REV · 11 live rules · maps 1:1 to 27957 ────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('SELL-REV','R19','Relative strength (63d)',       3,'27957'),
    ('SELL-REV','R5', 'Volume confirm',                3,'27957'),
    ('SELL-REV','R11','Turn-down',                     2,'27957'),
    ('SELL-REV','R10','Touched R1',                    2,'27957'),
    ('SELL-REV','R18','Momentum decay (dM 180d)',      2,'27957'),
    ('SELL-REV','R7', 'RSI weekly heat',               2,'27957'),
    ('SELL-REV','R1', 'Mood (any bearish)',            1,'27957'),
    ('SELL-REV','R2', 'Sector mo',                     1,'27957'),
    ('SELL-REV','R3', 'Peers',                         1,'27957'),
    ('SELL-REV','R9', '5m + VWAP',                     1,'27957'),
    ('SELL-REV','R12','Derivatives confirm',           1,'27957')
ON CONFLICT (bucket, rule_key) DO NOTHING;
