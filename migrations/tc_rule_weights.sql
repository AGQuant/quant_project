-- cc#1172 push 1/8 + cc#1173 reseed — TC_SCORE_V1 rule-weight registry.
-- WEIGHTS ARE THE FOUNDER LOCKS, not the 27957 first draft: BUY-REV 27974, BUY-MOM 27975,
-- SELL-REV 27976 (V2, sweep-selected), SELL-MOM 27977 (V2). Fable's original SELL seeds are
-- WITHDRAWN by 27976 — the sweep found V8 sell_reversal is short CONTINUATION in already-weak
-- stocks, not fade-the-rally-at-resistance, so the hot-RSI seeds scored the wrong archetype.
-- Calibration target A (27974) holds by construction: every weight > 0, minimum 0.5.
-- Denominators: BUY-MOM 27.0 · BUY-REV 21.0 · SELL-MOM 15.5 · SELL-REV 14.5.
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
-- THE GATE THAT PRODUCED THIS FILE. Push 1 enumerated the live rulebook and found NINE rule
-- instances live but unnamed in the 27957 draft — BUY-MOM R16/R20/R21/R23 and BUY-REV
-- R4/R8/R16/R20/R23. They were seeded at weight 1 and NAMED for a ruling rather than silently
-- defaulted. Fable ruled (forum 2979) and the founder locked (27974/27975), so all nine now carry
-- real weights: R16 and R21 at 1, R20 and R23 at 0.5, R4 and R8 at 1. Nothing here is a seed
-- guess any more — every row traces to a founder lock, named in its source column.
--
-- ON CONFLICT DO NOTHING is deliberate: this file SEEDS a fresh database. It does not re-weight an
-- existing one, because a live re-weight is an UPDATE the founder makes against the registry and a
-- migration must never silently overwrite a calibration made after it was written.
--
-- NOTE ON WEIGHT vs TODAY'S max. These are independent. `weight` is the registry's contribution to
-- the normalized 0-10 score; the rule's `max` in tc_v4_dual is what it can score today. R5 Volume
-- is max 1.0 on BUY-MOM but max 2.0 on the other three. The score divides credit by the rule's own
-- max before weighting, so halves stay halves and a max-2 rule cannot double-count. The rulebook
-- is NOT edited by this file — pass/fail logic and thresholds stay byte-identical.

CREATE TABLE IF NOT EXISTS tc_rule_weights (
    bucket      TEXT    NOT NULL,          -- BUY-MOM | BUY-REV | SELL-MOM | SELL-REV
    rule_key    TEXT    NOT NULL,          -- tc_v4_dual legacy rule id (R1, R5, R19 ...)
    rule_label  TEXT,                      -- the live label, for a readable registry
    weight      NUMERIC NOT NULL DEFAULT 1,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    source      TEXT,                      -- the founder-lock session_log id this weight came from
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (bucket, rule_key)
);

-- ── BUY-MOM · 19 live rules ─────────────────────────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('BUY-MOM','R19','Relative strength (63d)',3,'27975'),
    ('BUY-MOM','R5','Volume confirm',2,'27975'),
    ('BUY-MOM','R4','MAs 2of3',2,'27975'),
    ('BUY-MOM','R7','RSI frame',2,'27975'),
    ('BUY-MOM','R10','Style extra',2,'27975'),
    ('BUY-MOM','R18','Momentum (stock+sector M)',2,'27975'),
    ('BUY-MOM','R11','Location + room',2,'27975'),
    ('BUY-MOM','R1','Mood (fails)',1,'27975'),
    ('BUY-MOM','R2','Sector wk&mo',1,'27975'),
    ('BUY-MOM','R3','Peers',1,'27975'),
    ('BUY-MOM','R9','5m + VWAP',1,'27975'),
    ('BUY-MOM','R12','Derivatives confirm',1,'27975'),
    ('BUY-MOM','R17','Valuation (V)',1,'27975'),
    ('BUY-MOM','R22','52w index',2,'27975'),
    ('BUY-MOM','R24','Delivery confirm',1,'27975'),
    ('BUY-MOM','R16','Fib position (3M)',1,'27975'),
    ('BUY-MOM','R20','GVM trend (d180d)',0.5,'27975'),
    ('BUY-MOM','R21','dma_50 band',1,'27975'),
    ('BUY-MOM','R23','GVM floor',0.5,'27975')
ON CONFLICT (bucket, rule_key) DO NOTHING;

-- ── BUY-REV · 17 live rules ─────────────────────────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('BUY-REV','R19','Relative strength (63d)',1,'27974'),
    ('BUY-REV','R5','Volume confirm',3,'27974'),
    ('BUY-REV','R10','Style extra (mom_2d turn)',2,'27974'),
    ('BUY-REV','R17','Valuation (V)',1,'27974'),
    ('BUY-REV','R18','Momentum (stock+sector M)',1,'27974'),
    ('BUY-REV','R11','Location + room',3,'27974'),
    ('BUY-REV','R7','RSI monthly',2,'27974'),
    ('BUY-REV','R1','Mood (fails)',0.5,'27974'),
    ('BUY-REV','R2','Sector mo',0.5,'27974'),
    ('BUY-REV','R3','Peers',0.5,'27974'),
    ('BUY-REV','R9','5m + VWAP',2,'27974'),
    ('BUY-REV','R12','Derivatives confirm',0.5,'27974'),
    ('BUY-REV','R4','MAs graded',1,'27974'),
    ('BUY-REV','R8','Returns mo',1,'27974'),
    ('BUY-REV','R16','Fib position (3M)',1,'27974'),
    ('BUY-REV','R20','GVM trend (d180d)',0.5,'27974'),
    ('BUY-REV','R23','GVM floor',0.5,'27974')
ON CONFLICT (bucket, rule_key) DO NOTHING;

-- ── SELL-MOM · 12 live rules · maps 1:1 to 27957 ────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('SELL-MOM','R19','Relative strength (63d)',1,'27977'),
    ('SELL-MOM','R5','Volume confirm',2,'27977'),
    ('SELL-MOM','R4','MAs 2of3',1,'27977'),
    ('SELL-MOM','R7','RSI weekly cold',2.5,'27977'),
    ('SELL-MOM','R10','2-day fall band',3,'27977'),
    ('SELL-MOM','R18','Momentum decay (dM 180d)',1.5,'27977'),
    ('SELL-MOM','R11','Below PP + room to S1',1,'27977'),
    ('SELL-MOM','R1','Mood (any bearish)',0.5,'27977'),
    ('SELL-MOM','R2','Sector mo',0.5,'27977'),
    ('SELL-MOM','R3','Peers',0.5,'27977'),
    ('SELL-MOM','R9','5m + VWAP',1,'27977'),
    ('SELL-MOM','R12','Derivatives confirm',1,'27977')
ON CONFLICT (bucket, rule_key) DO NOTHING;

-- ── SELL-REV · 11 live rules · maps 1:1 to 27957 ────────────────────────────────────────────
INSERT INTO tc_rule_weights (bucket, rule_key, rule_label, weight, source) VALUES
    ('SELL-REV','R19','Relative strength (63d)',1.5,'27976'),
    ('SELL-REV','R5','Volume confirm',3,'27976'),
    ('SELL-REV','R11','Turn-down',1.5,'27976'),
    ('SELL-REV','R10','Touched R1',1.5,'27976'),
    ('SELL-REV','R18','Momentum decay (dM 180d)',2,'27976'),
    ('SELL-REV','R7','RSI weekly heat',1.5,'27976'),
    ('SELL-REV','R1','Mood (any bearish)',0.5,'27976'),
    ('SELL-REV','R2','Sector mo',0.5,'27976'),
    ('SELL-REV','R3','Peers',0.5,'27976'),
    ('SELL-REV','R9','5m + VWAP',1,'27976'),
    ('SELL-REV','R12','Derivatives confirm',1,'27976')
ON CONFLICT (bucket, rule_key) DO NOTHING;
