-- cc#2036 P1 · option_strategy_templates — 30 readymade option-strategy definitions (Custom
-- Builder / Readymade tabs, cc#2035 sprint).
-- Spec: session_log 45180 (OPTION_STRATEGY_BUILDER_V1_SPEC), 45192 (table_ddl, verbatim below).
--
-- ADDITIVE DDL ONLY: a NEW table, no ALTER, no index rebuild — outside MAINTENANCE_LOCK_RULE's
-- lock-taking set. Applied by CC through the normal migration path (this file, run once on the
-- DB) and seeded by option_strategy_templates_seed.py, which is idempotent (ON CONFLICT (name) DO
-- UPDATE) and safe to re-run.
--
-- legs is the TEMPLATE's own abstract leg list — {kind, side, offset_steps, qty} — offsets are
-- relative to ATM in strike-interval steps (0=ATM, +/-N = N strikes OTM/ITM; FUT legs always
-- offset_steps=0). This is deliberately NOT the same shape as a resolved/live leg (which carries a
-- real strike + premium) — POST /api/options/resolve (cc#2037) turns a template + a live ATM into
-- concrete legs; the template itself never stores a strike or a premium.

CREATE TABLE IF NOT EXISTS option_strategy_templates (
    id           serial PRIMARY KEY,
    name         text UNIQUE NOT NULL,
    view         text NOT NULL CHECK (view IN ('bullish','bearish','neutral')),
    sub_view     text CHECK (sub_view IN ('range','breakout') OR sub_view IS NULL),
    legs         jsonb NOT NULL,
    risk_profile text,
    description  text,
    sketch       text,
    source       text NOT NULL DEFAULT 'excel_2012',
    sort_order   int NOT NULL DEFAULT 0,
    is_active    bool NOT NULL DEFAULT true,
    created_at   timestamptz DEFAULT now()
);
