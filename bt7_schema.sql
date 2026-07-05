-- bt7_schema.sql — cc#218 (BT7 parity harness) DB objects.
-- Applied to Railway prod 05-Jul via run_sql; committed here for reproducibility.
-- Sandbox (RULING_A): bt7_sim = SELECT-only on public, ALL on harness. The harness
-- SET ROLEs into it (NOLOGIN — no credential in git) with search_path=harness,public,
-- so any live write from sim is a DB permission error (structurally unbypassable —
-- the id=1514 live-book-wipe guard). Verified: bt7_sim has_table_privilege INSERT on
-- public.v8_paper_positions = FALSE.

CREATE SCHEMA IF NOT EXISTS harness;

-- WRITE shadows (driven code writes resolve here via search_path). Same DDL as live.
CREATE TABLE IF NOT EXISTS harness.v8_metrics          (LIKE public.v8_metrics INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.v8_qualified        (LIKE public.v8_qualified INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.v8_paper_positions  (LIKE public.v8_paper_positions INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.v8_paper_trades     (LIKE public.v8_paper_trades INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.v8_paper_missed     (LIKE public.v8_paper_missed INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.v8_funnel_counts    (LIKE public.v8_funnel_counts INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.adr_intraday        (LIKE public.adr_intraday INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.app_config          (LIKE public.app_config INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.ops_log             (LIKE public.ops_log INCLUDING ALL);
-- INPUT materialization tables (per-run: bars + prior-day metrics baseline + pivots)
CREATE TABLE IF NOT EXISTS harness.intraday_prices     (LIKE public.intraday_prices INCLUDING ALL);
CREATE TABLE IF NOT EXISTS harness.v8_paper_pivots     (LIKE public.v8_paper_pivots INCLUDING ALL);

-- LIKE INCLUDING ALL copies column DEFAULTS that reference PUBLIC sequences (e.g. ops_log
-- shares session_log_id_seq). Repoint every harness serial default to a harness-OWNED
-- sequence so bt7_sim never touches public sequence state (keeps "SELECT-only public" pure).
DO $fix$
DECLARE r record; seqname text;
BEGIN
  FOR r IN SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema='harness' AND column_default LIKE 'nextval(%'
             AND column_default NOT LIKE 'nextval(''harness.%'
  LOOP
    seqname := r.table_name||'_'||r.column_name||'_seq';
    EXECUTE format('CREATE SEQUENCE IF NOT EXISTS harness.%I', seqname);
    EXECUTE format('ALTER TABLE harness.%I ALTER COLUMN %I SET DEFAULT nextval(%L)',
                   r.table_name, r.column_name, 'harness.'||seqname);
    EXECUTE format('ALTER SEQUENCE harness.%I OWNED BY harness.%I.%I', seqname, r.table_name, r.column_name);
  END LOOP;
END $fix$;

-- labeled per-run result archives (bt7_diff compares two labels; scratch shadows truncated each run)
CREATE TABLE IF NOT EXISTS harness.bt7_qualified AS SELECT ''::text AS run_label, * FROM harness.v8_qualified WITH NO DATA;
CREATE TABLE IF NOT EXISTS harness.bt7_positions AS SELECT ''::text AS run_label, * FROM harness.v8_paper_positions WITH NO DATA;
CREATE TABLE IF NOT EXISTS harness.bt7_trades    AS SELECT ''::text AS run_label, * FROM harness.v8_paper_trades WITH NO DATA;
CREATE TABLE IF NOT EXISTS harness.bt7_missed    AS SELECT ''::text AS run_label, * FROM harness.v8_paper_missed WITH NO DATA;
CREATE TABLE IF NOT EXISTS harness.bt7_runs (
    run_label TEXT PRIMARY KEY, target_date DATE, ticks INT, quals INT, entries INT,
    exits INT, gate_exits INT, ran_at TIMESTAMPTZ DEFAULT NOW(), source TEXT, notes JSONB,
    -- cc#218 hotfix: Railway stdout is invisible to the ops desk; the DB is not. A failed
    -- run records its true first exception here so bt7_diff never certifies a silent error.
    status TEXT DEFAULT 'ok', error_detail TEXT);
-- idempotent for a bt7_runs that predates the two columns above (also enforced in code at run start)
ALTER TABLE harness.bt7_runs ADD COLUMN IF NOT EXISTS status       TEXT DEFAULT 'ok';
ALTER TABLE harness.bt7_runs ADD COLUMN IF NOT EXISTS error_detail TEXT;

-- role + grants
DO $r$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bt7_sim') THEN CREATE ROLE bt7_sim NOLOGIN; END IF;
END $r$;
GRANT USAGE ON SCHEMA public TO bt7_sim;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO bt7_sim;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO bt7_sim;
GRANT USAGE, CREATE ON SCHEMA harness TO bt7_sim;
GRANT ALL ON ALL TABLES IN SCHEMA harness TO bt7_sim;
GRANT ALL ON ALL SEQUENCES IN SCHEMA harness TO bt7_sim;
ALTER DEFAULT PRIVILEGES IN SCHEMA harness GRANT ALL ON TABLES TO bt7_sim;
ALTER DEFAULT PRIVILEGES IN SCHEMA harness GRANT ALL ON SEQUENCES TO bt7_sim;
