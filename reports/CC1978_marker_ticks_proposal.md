# cc#1978 MARKER_TICKS_V1 — DDL proposal + ACT cost-gate resolved; STOPPED for founder's word

Per the card's own process: item 1 is a FOUNDER GATE ("propose the exact CREATE TABLE... and STOP
for the founder's word before running it"). This report is that stop. Nothing in `v8_marker_ticks`
exists yet — no DDL has been run.

## Census re-verification (Fable could not check `v8_pivot_star.py` — connector timeout)

Every line number the cc#1977 census cited in this file was re-read against the live file and
**all confirmed correct**: `EVAL_SCOPE = "positions"` at line 144, the `if EVAL_SCOPE ==
"universe":` branch at 240, the route `@router.get("/api/v8/pivot_star")` at 1054, the cc#1887
KeyError fix narrated in the comment at 907-918 (census said 909-915 — same area, correct). No
correction needed this time; the census held up.

**Confirmed directly, not assumed:**
- `run_tick()` (line 860) is the one function `scheduler.py`'s `_bg_pivot_star()` calls, and it
  already evaluates and writes THREE families every tick: `evaluate()` (stars, line 873),
  `evaluate_activity()` (acts, 919), `evaluate_dma_state()` (dmas, 948) — CHAN is a fourth family,
  written by a **separate** job/file (`v8_channel_5m.py`, its own `_bg_channel_5m()`), not this
  one. TCS (`tc_universe_ticks`) is a **fifth**, already-existing table (cc#1862/cc#1995, this
  session) — for this card TCS just needs a READ of what already exists, not a new write.
- `evaluate_dma_cross()` (line 492, the ORIGINAL cross-only family) is superseded by
  `evaluate_dma_state()` (line 583, cc#1682) and **not called from `run_tick()` at all** — DMA for
  this card means `evaluate_dma_state()`, confirmed by reading `run_tick()` itself rather than
  inferring from the family name.
- STARS and DMA are already fully batched internally: every supporting read in `evaluate()` and
  `evaluate_dma_state()` uses `= ANY(%s)` (pivots, metrics, the touch-test CTE, the close-history
  window). Flipping `EVAL_SCOPE` from `"positions"` to `"universe"` changes only the CANDIDATE SET
  query (one line, already written and switchable — line 240-241) — no per-symbol loop hides
  anywhere in either function. Item 5's claim holds.

## Item 6 — ACT cost gate: RESOLVED, not punted

The census's warning was correct on the code: `evaluate_activity()` (line 366) loops
`for sym in syms: r6_read(cur, sym); _ad_21d(cur, sym)` one symbol at a time (`deliv_ratio_batch`
is already a batch call — only these two are per-symbol). At 208 symbols this is exactly the
~416-call shape the census measured as unusable on a 5-min beat.

**Both reads now have genuine batch forms, verified against real data before being written into
this file, not merely composed and assumed correct:**

- **`_ad_21d_batch`** (new, `deriv_metrics.py`) — one windowed query (`ROW_NUMBER() OVER
  (PARTITION BY symbol ...)`) instead of N single-symbol queries, the identical pattern
  `v8_pivot_star.evaluate_dma_state` already uses in this same file for its own close-history
  read. Verified: the batch query's rows for NATIONALUM/RELIANCE match the single-symbol query's
  rows exactly (checked directly against `raw_prices`, 11-Sep).

- **`eod_rvol_pair_batch`** (new, `rvol_engine.py`) — **not** a composition of the existing
  `day_rvol_batch`/`_day_ratios` (that reads a *different* source, `intraday_prices` +
  `rvol_profiles`, which a comment on `eod_volume_ratio` asserts is "algebraically identical" to
  the raw_prices-window form for a closed session — measured NOT bit-identical: RELIANCE 11-Sep,
  `rvol_profiles` 15:25 `avg_cum_vol` 10,731,480 vs this window's own `a21` 10,731,748 — same
  formula, two independently-computed snapshots, a real if tiny drift). Instead this is `PARTITION
  BY symbol` added to `eod_rvol_pair`'s own exact SQL — the same table, the same window, one round
  trip. **Verified byte-identical** to `eod_rvol_pair` on real data: ran both forms side by side
  for TCS, NATIONALUM, RELIANCE, INFY (11-Sep) — `rv`/`vp`/`price_date`/`prev_d` matched to full
  decimal precision on every symbol.

- **`r6_read_batch`** (new, `r6_volume.py`) — composes the two above (plus the already-shipped
  `live_rvol_batch`) reproducing `r6_read`'s exact branch (does the EOD pair's own latest session
  equal the live anchor? read the LAG side; else read the pair's own latest directly), verbatim
  against `r6_read`'s source. One inherited approximation, not a new one: `live_rvol_batch` already
  anchors every symbol to one shared "latest session" date rather than each symbol's own — this
  batch form reuses that same shared anchor for its branch test, matching a convention
  `live_rvol_batch` already ships with today, not something this card adds.

None of `r6_read`, `_ad_21d`, `eod_rvot_pair`, `live_rvol`/`live_rvol_batch` were changed — every
new function is a pure addition, existing callers untouched.

**Not yet done, and deliberately not done this pass:** wiring these batch forms INTO
`evaluate_activity()`'s live path, and flipping `EVAL_SCOPE`. Both are downstream of the DDL —
there is no `v8_marker_ticks` table yet to write universe-wide rows into, and changing a function
that runs live every 5 minutes today ahead of having anywhere to verify the new output lands is
exactly the kind of change that should wait for the gate, not race it.

## Item 1 — the DDL, refined by one column the card's own verify needs

The card's proposed shape is missing a column its OWN verify V2 requires: *"fired=false rows exist
and outnumber fired=true — proof that 'evaluated and did not fire' is being recorded."* There is no
way to prove that without a `fired` column. Proposed:

```sql
CREATE TABLE IF NOT EXISTS v8_marker_ticks (
  symbol       TEXT NOT NULL,
  ts           TIMESTAMPTZ NOT NULL,
  family       TEXT NOT NULL,          -- star | act | dma | tcs | chan
  fired        BOOLEAN NOT NULL,       -- V2's own verify needs this column to exist at all
  direction    TEXT,
  colour       TEXT,
  level_name   TEXT,
  level_value  NUMERIC,
  detail       JSONB,
  PRIMARY KEY (symbol, ts, family)
);
CREATE INDEX IF NOT EXISTS v8_marker_ticks_ts_symbol_idx ON v8_marker_ticks (ts DESC, symbol);
```

One `CREATE TABLE IF NOT EXISTS` + one `CREATE INDEX IF NOT EXISTS`, no `ALTER TABLE` anywhere —
`MAINTENANCE_LOCK_RULE` (cc#351) respected; this is a brand-new table, not a lock-taking op on an
existing one, same reasoning already used for `tc_universe_rule_ticks` (cc#1995, this session).
**Not run** — waiting on the founder's word per the card's own item 1.

`detail JSONB` carries whatever the family-specific evaluator already returns beyond the shared
columns (e.g. star's `pp`/`cmp_at_star`/`pct_from_level`/`near_pp`/`day_1d`/`mom_2d`/`dma_50`/
`touched_dates`/`basket`; act's `checks[]`/`facts[]`; dma's `pp` (=20DMA) /`cmp_at_star`/`day_1d`;
tcs' rule-level detail already sitting in `tc_universe_ticks`) — no second bespoke column set per
family, matching the design's own instruct to keep one shared table.

## What ships once the DDL is approved (not built yet, so the plan is stated rather than pushed)

1. `ensure_schema()` gets the table (mirrors the DDL above exactly).
2. `run_tick()` gains a `v8_marker_ticks` insert after each of its three existing writes — same
   transaction, same `ts` (localised: `_ist_now()` returns a NAIVE IST value in this file exactly
   like the pre-fix `v8_channel_5m` case the card warns about; the write path does
   `ts_tz = IST.localize(ts) if ts.tzinfo is None else ts` before the INSERT, copying
   `v8_channel_5m.compute_channels`'s own guarded pattern verbatim rather than a bare
   `.replace(tzinfo=IST)`, which pytz would resolve to the wrong +05:53 LMT offset).
3. `EVAL_SCOPE` flips to `"universe"` for `evaluate()`/`evaluate_dma_state()` (FOUNDER_WORD_10SEP
   already grants this explicitly).
4. `evaluate_activity()` switches its per-symbol loop to `r6_read_batch` + `_ad_21d_batch` (both
   ready now) so the universe-wide ACT read is one batched round trip per tick, not ~416 calls.
5. TCS: read `tc_universe_ticks` (already all ~207 symbols, all 4 buckets, cc#1995) and apply the
   existing amber condition — zero new compute, per the card.
6. 30-day purge, same last-tick-of-day trigger as `tc_universe_ticks`'/`tc_universe_rule_ticks`'
   own 90/30-day purges (cc#1862/cc#1995 precedent) — ships WITH the writer, not a later card.
7. `scheduler_master` row + first-run evidence, once there is a real tick to point at.
8. `/api/v8/pivot_star` gains an additive per-symbol read from the new store (item 9) — the
   existing book-scoped response shape is untouched for current consumers.

## Verify (this pass)

- `ast.parse` + `py_compile` clean on `deriv_metrics.py`, `rvol_engine.py`, `r6_volume.py`.
- `_ad_21d_batch`: batch query rows checked against the single-symbol query's own rows on real
  data (RELIANCE, NATIONALUM) — match.
- `eod_rvol_pair_batch`: full-precision match against `eod_rvol_pair`'s own SQL on 4 real symbols
  (TCS, NATIONALUM, RELIANCE, INFY), same session date.
- `r6_read_batch`'s branch logic checked line-for-line against `r6_read`'s source; the one
  approximation it carries (`live_rvol_batch`'s shared anchor) is `live_rvol_batch`'s own existing
  convention, not new.
- No DDL run. No existing function's behaviour changed — `evaluate_activity()`, `EVAL_SCOPE`,
  `r6_read`, `_ad_21d`, `eod_rvol_pair` all untouched. Nothing under `worker/**`.

## What this pass does NOT close

Items 2 (timestamp write, needs the table), 3/4 (CHAN/TCS persistence, needs the table), 7
(retention, needs the table), 8 (liveness evidence, needs a real tick), 9 (distribution endpoint,
needs real rows to read) all wait on the DDL. Item 1's founder word is the one thing blocking every
one of them — that is the point of a gate, not a gap in this pass.

Files changed this push: `deriv_metrics.py`, `rvol_engine.py`, `r6_volume.py` (three new, additive
batch functions) + this report. Card **NOT** done, **NOT** blocked in the sense of "waiting on
someone else's unrelated work" — waiting specifically on the founder's word on the DDL above.
