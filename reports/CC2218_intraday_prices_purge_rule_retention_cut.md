# cc#2218 -- DB retention: intraday_prices rolling purge + tc_universe_rule_ticks retention cut

## What changed

Two constants and one new `DELETE`, all inside `tc_universe_ticks.py`'s existing `run_tick()` job,
riding the SAME 15:20 IST last-tick block its own purges already use -- no new scheduled job, no
schema change, no `ALTER`, never `VACUUM FULL` or any locking op (MAINTENANCE_LOCK_RULE cc#351):

1. **`intraday_prices` gets its first purge ever.** New constant
   `INTRADAY_PRICES_RETENTION_MONTHS = 12`; `DELETE FROM intraday_prices WHERE ts < NOW() -
   INTERVAL '12 months'`, run and committed inside the existing last-tick transaction, reported to
   `ops_log` in the same `tc_universe_tick` entry the sibling purges already write to (new
   `intraday_purged` key).
2. **`RULE_RETENTION_DAYS` cut 30 -> 3** on `tc_universe_rule_ticks`. One constant, the existing
   purge mechanism (same table, same last tick, shipped with the writer per cc#1995) is otherwise
   untouched.
3. **`tc_universe_ticks.RETENTION_DAYS` (90) is untouched** -- confirmed by diff, not just by
   intent; the founder reconfirmed 90 on 18-Sep and this card explicitly protects it as
   `do_not_touch`.

## Why `tc_universe_ticks.py` is the right host

The card's own scope names this file's `run_tick()` as one of two reference examples for "ship
inside an existing daily job." Since the card was already changing `RULE_RETENTION_DAYS` inside
this exact function, adding `intraday_prices`'s purge to the SAME last-tick block (rather than
reaching into a third module, or the `worker/**` feed service, which carries its own separate
deploy-window rule) keeps the change to one file, reuses an already-proven pattern
(`tc_universe_ticks`'s own 90-day purge, `v8_marker_ticks._purge()`), and avoids touching anything
on the live-feed worker path.

## Checked before shipping, not just asserted

- `EXPLAIN SELECT 1 FROM intraday_prices WHERE ts < NOW() - INTERVAL '12 months'` -- **Index Only
  Scan** on the existing `idx_intraday_symbol_ts (symbol, ts DESC)` index, low cost estimate. No
  new index needed, no table scan.
- `SELECT COUNT(*) ... WHERE ts < NOW() - INTERVAL '12 months'` -- **13,167 rows** at the current
  boundary (small; the founder's own manual one-time cleanup earlier today already trimmed the
  table to exactly a 12-month boundary, so this and every future run is a small daily increment,
  not a bulk delete).
- Diffed the whole file: `tc_universe_ticks.RETENTION_DAYS` (90) is the only OTHER retention
  constant in this file, and it is byte-for-byte unchanged.

## Validation done in this sandbox

- `ast.parse` on `tc_universe_ticks.py` after every edit.
- `python3 -c "import tc_universe_ticks"` -- clean, no DB needed at import time.
- `pytest tests/` -- 121 passed (unchanged from before this card; `run_tick()` has no pre-existing
  unit tests to extend -- it is bulk-scoring + DB writes throughout, no pure helper to isolate the
  way `v8_endpoints.py`/`v8_live_tc.py` already had for cc#2214/cc#2215's tests).
- Live, read-only `EXPLAIN`/`COUNT` verification against Railway (above) -- this IS the DB check
  the sandbox can do for a query shape, short of running the actual write.

## What could not be verified from this sandbox

The purge only fires once per day, on the market's last tick (15:20 IST) -- today's already
closed, so the first live run is the next trading day's 15:20 IST, **Monday 21-Sep-2026** (checked
`nse_holidays.is_trading_day()` directly: 19-Sep and 20-Sep are Sat/Sun). Per ENGINE_LIVENESS_RULE,
this is stated plainly: **built and registered, not yet confirmed live** -- first-run evidence
(`ops_log` `intraday_purged` count, `intraday_prices` row count/MIN(ts) the following day) follows
after that tick, not claimed here.

## Verify checklist (from the card's own spec)

- [ ] Confirm the `intraday_prices` purge runs once per day, logs its deleted count to `ops_log`,
      and the table's `MIN(ts)` stays within 12 months of today the following day.
- [ ] Confirm `RULE_RETENTION_DAYS` reads 3 live and `tc_universe_rule_ticks` stabilises near
      ~2.6M rows after 4 trading days (checked directly in source: reads 3 now).
- [ ] Confirm `tc_universe_ticks.RETENTION_DAYS` still reads 90 and its row count is unaffected --
      confirmed by diff (untouched).
- [ ] Confirm the Check tab rule bars render for the CURRENT tick after the first post-deploy
      write (the truncate the founder already ran means no rule history before the next market
      open -- expected, not a regression, per the card's own verify note).
