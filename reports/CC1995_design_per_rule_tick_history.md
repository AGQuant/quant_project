# cc#1995 — design: per-rule tick history for TC (`tc_universe_rule_ticks`)

**Status: DESIGN ONLY, posted per the card's own "design first, post it, then build" sequencing.
No code or schema change in this push.** The BUILD half (items 1's `CREATE TABLE` + the writer
change, items 2-4) stays parked for the card's own window ("before 09:15 or after 15:30 IST") —
today's after-15:30 slot, alongside cc#1996/cc#1999/cc#1993/cc#2000's other live-path work.

## Why this exists (unchanged from the card)

cc#1991 found the Check tab header printing a rule-credit re-derivation (64.5) beside the card's
own stored score (61.0) for the same tick — both right for their own instant, wrong side by side.
Option (b), already shipped, states both moments. This card removes the second moment: store the
per-rule credits AT WRITE TIME so the header reads the same stored tick the card does, rather than
recomputing anything at render.

## Grounded in the real writer, not invented from scratch

Read `tc_universe_ticks.py` in full (177 lines) before designing anything. The existing aggregate
writer (`run_tick()`, cc#1862, founder-released 09-Sep):

- Scores the **full active futures universe**, all 4 buckets x 2 sides, via `tc_v4_dual.score_card`
  over `tc_v4_scan._load_bulk(cur)` — the same shared scorer the book's own scan and the Check tab
  read, not a second compute path.
- Writes via `COPY ... FROM STDIN` into a `CREATE TEMP TABLE ... ON COMMIT DROP` staging table,
  then `INSERT ... SELECT ... ON CONFLICT (symbol, ts, bucket) DO NOTHING` — one transaction.
- Retention: **90 days**, purged by a plain `DELETE` (never `VACUUM FULL`, per
  `MAINTENANCE_LOCK_RULE`) on the 15:20 IST last tick of each day, same job, same run.
- Stops writing after 15:20 IST — the founder-mandated last tick.

The new table and its write should be **the same shape**: same transaction, same `ts`, same
COPY-staging pattern, appended right after the existing aggregate insert (~line 154 of
`tc_universe_ticks.py`) — not a second writer, not a second job.

## 1. Table: `tc_universe_rule_ticks`

```sql
CREATE TABLE IF NOT EXISTS tc_universe_rule_ticks (
    symbol    TEXT        NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    bucket    TEXT        NOT NULL,
    rule_key  TEXT        NOT NULL,
    credit    NUMERIC,
    max       NUMERIC,
    weight    NUMERIC,
    PRIMARY KEY (symbol, ts, bucket, rule_key)
);
CREATE INDEX IF NOT EXISTS tc_universe_rule_ticks_symbol_bucket_ts_idx
    ON tc_universe_rule_ticks (symbol, bucket, ts);
```

`symbol, ts, bucket` mirror `tc_universe_ticks` exactly (same PK prefix, so the two tables join
trivially on their shared key). The read index is `(symbol, bucket, ts)` rather than
`(ts, symbol)` like the parent table — the parent's index serves "every symbol at this tick"
(dashboard-style sweeps); this table's primary read (item 3) is "every rule for ONE symbol/bucket
at a KNOWN ts", which `(symbol, bucket, ts)` serves directly without a filter step.

**Where credit/max/weight come from — checked, not assumed:** `tc_v4_dual.score_card()` already
builds a per-rule list internally (`tc_v4_dual.py:150`, `{"rule": rid, "label": label,
"credit": ...}`, summed at `:1688` into the single `score100` the aggregate table stores) — the
per-rule numbers this card needs are already computed on every tick, just not currently returned
by `score_card()`. **This needs a small, additive return-shape change** (return the existing
`rules` list alongside the existing aggregate fields) — not a formula change, so it stays inside
the card's own `do_not_touch` ("the canon rule set, weights and thresholds ... this module calls
score_card() unmodified, it does not change what TC computes"): nothing about what is computed
changes, only what is returned. `max` and `weight` per rule read from `tc_rule_weights` the same
way `score_card()`'s own internals already do (`tc_v4_dual.py:1578`, `SELECT bucket, rule_key,
weight FROM tc_rule_weights WHERE active`) — this table does not re-derive them a second way.

## Sizing — measured against real rows, not the card's own rough estimate

The card's evidence line estimates "208 symbols x 4 buckets x ~19 rules x 75 ticks/day." Checked
against real data before proposing retention:

| Input | Card's estimate | Verified today |
|---|---|---|
| Symbols | 208 | **207** (`COUNT(DISTINCT symbol)`, tc_universe_ticks, 10-Sep) |
| Ticks/day | 75 | **71** (`COUNT(DISTINCT ts)`, same day, 09:15-15:20 IST) |
| Rules, per bucket | ~19 flat | **20 / 19 / 9 / 11** (BUY-MOM / BUY-REV / SELL-MOM / SELL-REV, `tc_rule_weights WHERE active`) — NOT uniform |

Real rows/tick = 207 symbols x 59 rules (sum across the 4 buckets, not 19 x 4) = 12,213.
Real rows/day = 12,213 x 71 ticks = **867,123**.
At the card's own proposed 30-day rolling retention: **~26.0M rows steady-state**
(867,123 x 30) — about 27pct smaller than the card's own back-of-envelope (208x4x19x75x30 =
35.6M), because two of the four buckets (SELL-MOM 9, SELL-REV 11) run far fewer rules than the
"~19 flat" estimate assumed.

**Retention recommendation: 30 days, as the card proposes** — matching `MARKER_TICKS_V1`'s own
window, and deliberately shorter than the parent aggregate table's 90 days: the per-rule grain is
~59x the aggregate table's own row count per tick (12,213 vs 207x4=828), so holding it to the same
90 days would mean ~78M rows steady-state instead of ~26M. 30 days keeps the newest month's rule
history (covering the Check tab's own use case — explaining THIS tick's number) without carrying
three months of per-rule granularity nothing currently reads past the aggregate.

**On the 90s hard gate (item 4):** the existing aggregate COPY already moves ~828 rows/tick
comfortably inside the existing cadence. COPY throughput scales far better than row count scales
linearly, so 12,213 rows/tick (~15x the current volume) is very unlikely to threaten a 90s budget
on its own — but this is a expectation, not a measurement, and ENGINE_LIVENESS_RULE's own
discipline is to measure the real first tick rather than trust the expectation, so this stays an
explicit build-time verification step (below), not a claim made now.

## 2. Invariant check

Proposed: `100 * SUM(weight * credit/max) / SUM(weight)` over the rule rows for a given
`(symbol, ts, bucket)` must equal that same key's `score100` on the aggregate table, to 0.05 —
exactly what `score_card()` already computes internally (this is the SAME sum the function already
performs to produce `score100`; the invariant is checking the two tables agree, not re-deriving
the formula a second way). Proposed as a **write-time assertion inside the same transaction** the
rows are written in (compute the sum from the just-built `rows` list before the COPY, compare to
the aggregate `score100` already held in memory) rather than a separate periodic job — catches a
mismatch on the very tick it would happen, not on a later sweep. A mismatch beyond tolerance logs
to `ops_log` (same pattern `_oplog()` already uses in this file) and skips writing that one
symbol/bucket's rule rows for that tick (the aggregate row still writes — a rule-row gap is
visible as missing data, not as a silently wrong number). Proposed as a pytest-style test fixture
using a handful of real stored ticks (via `tc_v4_scan._load_bulk` against known symbols) for CI,
separate from the runtime assertion.

## 3. `app_check_endpoints.py` read-path change

Not designed in full detail here (out of the "design first" scope for THIS post — this section
states intent, not a diff). Once the table exists and has real data: the rule-list read moves from
its current render-time `score_card()` re-derivation (the exact gap cc#1991 found and labeled,
`app_check_endpoints.py`, option (b) already shipped) to a `SELECT rule_key, credit, max, weight
FROM tc_universe_rule_ticks WHERE symbol=%s AND bucket=%s AND ts = <the same ts the card's own
score100 came from>` — one query, one as-of, the exact ts already available from the aggregate
row the card is built from. `rules_as_of` (added in cc#1991) becomes unnecessary once this lands,
since there is only one moment left to state.

## 4. ENGINE_LIVENESS_RULE gate plan

- A `scheduler_master` row is NOT proposed as a new job — the write rides inside
  `tc_universe_ticks`'s existing `run_tick()` / `_bg_tc_universe_tick` registration, so it inherits
  that job's existing liveness tracking rather than needing a second registry row for the same
  tick.
- First-run evidence to post once built: distinct symbols in `tc_universe_rule_ticks` at the first
  real tick (expect 207, matching the aggregate table's own count for the same ts), rows written
  (expect ~12,213, per the sizing above — stated as an expectation to check against, not asserted
  in advance), and the tick's real wall-clock duration next to the aggregate write's own baseline
  (today's `tc_universe_ticks` tick duration, to be read fresh at build time) — not the 90s cap
  itself, which is a ceiling to confirm was not approached, not a number to report as if it were
  the measurement.

## What is NOT being decided here

Retention (30d) and the invariant tolerance (0.05) are **proposals**, matching the card's own
"propose retention" wording — not rulings. Both are cheap to revise before the build lands (no
data exists yet to migrate). The `score_card()` return-shape addition is the one piece that
touches code outside `tc_universe_ticks.py` itself; flagging it plainly here rather than
discovering it mid-build, since `tc_v4_dual.py` carries its own `do_not_touch` weight given how
many surfaces read it.

## Verify

- No `CREATE TABLE`, no code change, no DB write in this push — the report itself is the
  deliverable, per the card's own sequencing.
- Sizing numbers (207 symbols, 71 ticks/day, 20/19/9/11 rules per bucket) come from live queries
  run this tick, not memory or estimate — stated as query results, not asserted.

Nothing under `worker/**` (confirmed: the tick writer is `tc_universe_ticks.py` at the repo root,
dispatched by `scheduler.py` — neither is under `worker/**`, so the eventual build is not subject
to `FEED_WORKER_DEPLOY_RULE`, independent of the card's own live-tick-path window). Card **NOT**
set done — this is the design half only; Fable/founder review before the build half proceeds.
