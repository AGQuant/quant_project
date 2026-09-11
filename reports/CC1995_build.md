# cc#1995 build — tc_universe_rule_ticks, items 1+2 landed; item 3 deferred to real data

Per the posted design (`reports/CC1995_design_per_rule_tick_history.md`, sha `871df45`) and
Fable's order (log 6354, item 4, third push).

## Item 1 — the table + the writer

`tc_universe_rule_ticks` created exactly per the design: `(symbol, ts, bucket, rule_key, credit,
max, weight)`, PK `(symbol, ts, bucket, rule_key)`, index `(symbol, bucket, ts)` for the "every
rule for one symbol/bucket at a known ts" read `app_check_endpoints.py` will eventually do. DDL
run directly (a plain `CREATE TABLE IF NOT EXISTS` on a brand-new table, not a lock-taking op on
existing data — `MAINTENANCE_LOCK_RULE` does not apply) so both the schema and the code's own
`_ensure_table()` addition are confirmed correct now rather than only on the first live tick.

The write rides inside `tc_universe_ticks.py`'s existing `run_tick()` — no second job, no new
`scheduler_master` row, per the design's own plan. Same transaction, same `ts`, same
COPY-into-temp-staging-then-`INSERT...ON CONFLICT DO NOTHING` pattern the aggregate table's own
write already uses, added right after it inside the same cursor block so both commit together or
not at all.

**Where credit/max/weight come from — confirmed, not re-guessed:** `score_card()` (`tc_v4_dual.py`)
already returns the full per-rule list under `card["rules"]` (line 1718, `card = {..., "rules":
rules}`) — the design doc's own note that this "needs a small additive return-shape change" turned
out to be **wrong on inspection**: the data was already exposed, nothing in `tc_v4_dual.py` needed
touching. `weight` is read from `_rule_weights()` (also already public enough to import, same
module), fetched **once per tick** — its own 60s cache means every card scored in the same tick
reads the identical weight snapshot, so the weight this table stores is provably the same one
`score100` was actually computed from, not a second, possibly-stale read.

## Item 2 — the invariant, checked at write time

`100 * SUM(weight * credit/max) / SUM(weight)` computed per (symbol, bucket) from the rule rows
about to be written, compared to that card's own `score100`, tolerance 0.05. **Verified to mirror
`score_card`'s exact two-step rounding** (round score10 to 2dp, then round score100 = score10×10
to 1dp — a single combined rounding step disagrees with the real formula by a few hundredths in
edge cases, so the code does both steps explicitly): ran 2,000 randomized rule sets through both
formulas in isolation, **zero false mismatches**. A deliberate corruption test (swapping one
rule's credit and max) produced true score100 66.7 vs recomputed 82.9 on the corrupted rows —
correctly caught, well outside tolerance.

On a real mismatch: that symbol/bucket's rule rows are withheld (not written), the aggregate row
is unaffected (it already wrote from `score_card`'s own number, untouched), and the mismatch is
logged to `ops_log` (capped at 20 samples per tick so one bad tick cannot flood it) plus a
`log.warning`. This is a write-time gate, not a periodic sweep — a real mismatch is visible the
tick it happens.

## Retention

`RULE_RETENTION_DAYS = 30`, purged on the same 15:20 IST last-tick-of-day trigger the parent
table already uses, same plain `DELETE` (never `VACUUM FULL`, `MAINTENANCE_LOCK_RULE` respected),
ships with the writer per the same "purge ships WITH the writer" rule cc#1862 set for the parent
table. Deliberately shorter than the parent's 90 days — the per-rule grain is far larger per tick;
see the design doc's sizing math (~26M rows at 30 days vs ~78M at 90).

## Item 3 — NOT done this push, and here is why

`app_check_endpoints.py`'s read-path switch (render-time re-derivation → reading this table)
needs **real rows to read**. This job only runs during market hours (`_is_cash_continuous`, stops
15:20 IST) — today's window is already closed by the time this push lands. The table exists,
empty, ready for the next trading session. Switching a live read path to an empty table would be
exactly the kind of "built-and-registered is not live" gap `ENGINE_LIVENESS_RULE` exists to catch
— not doing that. Item 3 is the natural next push, gated on the first real tick's evidence.

## First-run evidence — what to check, not yet checked

`_bg_tc_universe_tick` rides the existing job's own `scheduler_master` row and cadence (09:15-15:20
IST, `m % 5 == 0`), so no new registry row to watch. First-run evidence to post once the next
trading session runs: distinct symbols in `tc_universe_rule_ticks` at the first real tick (expect
~207, matching `tc_universe_ticks`'s own count for the same `ts`), `rule_rows_written` from the
tick's own return dict (expect ~12,200 per the design's sizing), `rule_mismatches` (expect 0;
if not 0, that is the invariant doing its job, not a build defect), and the tick's real wall-clock
duration next to the aggregate write's own (to confirm the added COPY does not meaningfully change
the tick's cost — COPY throughput scales far better than linearly with row count, so this is
expected, not yet measured on real data).

## Verify

- `ast.parse` + `py_compile` clean on `tc_universe_ticks.py`.
- DDL run directly, both indexes confirmed present (`information_schema.columns`, `pg_indexes`).
- Invariant formula verified in isolation (2,000 trials, 0 false positives; 1 deliberate
  corruption correctly caught) — a pure-arithmetic check, no live data needed for this part.
- `scheduler.py`'s `_bg_tc_universe_tick` dispatcher untouched and still compatible: it only reads
  `res.get("ok")`, which the new return dict still carries unchanged.

Nothing under `worker/**`. Only file changed: `tc_universe_ticks.py` (+ this report + the DDL run
directly for the table itself). Card **NOT** set done — Fable verifies, and item 3 plus the
first-run evidence both wait on the next trading session (today's market-hours window for this
job has already closed).
