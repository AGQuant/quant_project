-- cc#1589 P4 · v8_paper_trades: one closed row per trade, enforced by the database.
--
-- STATUS: PROPOSED. NOT APPLIED. Founder gate (MAINTENANCE_LOCK_RULE, CLAUDE.md rule 10):
-- CREATE INDEX takes a lock, so this runs from the Railway console only, on a weekend, after
-- Arpit approves. The app never executes this file. run_sql hard-blocks it by design.
--
-- WHY: on 31-Aug-2026 the EOD exit fallback was dispatched twice on the same scheduler tick
-- (primary at scheduler.py h==2,m==0 + the cc#841 catch-up sweep on the same m%5==0 tick).
-- Both threads read the same open positions and both wrote a closed row: 7 duplicate pairs,
-- ids (644,645) (646,647) (648,649) (650,651) (652,653) (654,655) (656,657), every one with
-- exit_ts 2026-08-31 15:30:00. The app fix (v8_paper._close_position: claim the position with
-- DELETE ... RETURNING before the INSERT, plus SELECT-before-INSERT on the trade key) makes the
-- second write a logged no-op. This index is the belt to that brace: even a code path that
-- forgets the guard cannot land a second row.
--
-- KEY: (symbol, side, basket, entry_ts). A paper trade is opened exactly once at one instant,
-- so two closed rows with the same key are always the same trade written twice.
--
-- PRE-CONDITION (must be done FIRST, by Fable under the founder gate; the index build fails
-- while the 7 duplicate rows exist): delete the 7 higher-id twins 645,647,649,651,653,655,657.
-- Check there are no other collisions before building:
--
--   SELECT symbol, side, basket, entry_ts, COUNT(*) FROM v8_paper_trades
--   GROUP BY 1,2,3,4 HAVING COUNT(*) > 1;
--
-- CONCURRENTLY: builds without blocking writes to the table (cannot run inside a transaction
-- block; run it as a single statement). Expected time on ~130 rows: well under a second.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_v8_paper_trades_close_key
    ON v8_paper_trades (symbol, side, basket, entry_ts);

-- ROLLBACK (if ever needed):
--   DROP INDEX CONCURRENTLY IF EXISTS ux_v8_paper_trades_close_key;
