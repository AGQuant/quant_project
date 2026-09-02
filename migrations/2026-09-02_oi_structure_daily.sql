-- cc#1575 P1 · oi_structure_daily — one row per (underlying, session date, snapshot kind).
-- Spec: session_log 36283 (OI_STRUCTURE_INTERPRET_V1), snapshot_table.
--
-- ADDITIVE DDL ONLY: a NEW table, no ALTER, no index rebuild — outside MAINTENANCE_LOCK_RULE's
-- lock-taking set. Applied by CC through the normal migration path (this file, run once on the
-- DB) and mirrored by oi_structure.ensure_table(), which the snapshot JOBS call (never a request
-- handler) so a fresh environment self-heals on the first scheduled run.
--
-- Two snapshots per session: 11:00 IST (mid) and 15:25 IST (close). next_day_* are filled T+1
-- by fill_next_day from intraday_prices (index SPOT sources only, never futures).

CREATE TABLE IF NOT EXISTS oi_structure_daily (
    underlying        TEXT        NOT NULL,
    d                 DATE        NOT NULL,
    snapshot_kind     TEXT        NOT NULL,          -- 'mid' | 'close'
    snapshot_ts       TIMESTAMP,                     -- the option_chain tick the row was built from (IST wall-clock)
    expiry            DATE,
    spot              NUMERIC,
    spot_basis        TEXT,                          -- 'live' (cmp_prices) | 'intraday_bar' | 'prev_close'
    max_pain          NUMERIC,                       -- NULL when max_pain.py's guard refuses
    call_wall         NUMERIC,
    call_wall_oi      BIGINT,
    put_wall          NUMERIC,
    put_wall_oi       BIGINT,
    second_call_wall  NUMERIC,
    second_put_wall   NUMERIC,
    pcr               NUMERIC,                       -- chain-wide PE OI / CE OI, NULL when CE OI is zero
    one_sided         BOOLEAN     NOT NULL DEFAULT FALSE,
    scenario          TEXT,                          -- PIN | RANGE | ABOVE_CALL_WALL | BELOW_PUT_WALL | MAX_PAIN_FAR | ONE_SIDED
    mp_dist_pct       NUMERIC,                       -- (max_pain - spot) / spot * 100
    range_width_pct   NUMERIC,                       -- (call_wall - put_wall) / spot * 100
    days_to_expiry    INTEGER,
    next_day_pct      NUMERIC,                       -- next session close vs this spot, filled T+1
    next_day_high_pct NUMERIC,
    next_day_low_pct  NUMERIC,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (underlying, d, snapshot_kind)
);
