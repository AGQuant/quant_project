# cc#2085 (P0) — V12 backtest look-ahead bias: discovery phase (steps 1-3, per the card's own gate)

Built under explicit founder authorization (Fable unavailable, "clear the queue push all to main,
founder decision"). This card's own gate: steps 1-3 are read-only, report before any build —
**and step 5 explicitly says do not build the fix if it turns out blocked on more data
collection.** It does. This report is the discovery-phase deliverable; no code changes ship
with it, correctly, per the card's own instruction.

## Item 1 — confirmed by reading the code, exact citation

`v12_backtest.py`'s `_resolve_universe(cur, universe_ref)` (line 94) is called **exactly once**,
at line 178, **before** the rebalance-date walk begins (`rebals = _rebalance_dates(...)` runs
afterward, at line 197). For a filtered (GVM/fundamental) universe it runs:

```python
cur.execute("SELECT g.symbol " + _UNI_BASE + where + " ORDER BY g.gvm_score DESC NULLS LAST", params)
```

— against `gvm_scores`, the **current, today-only snapshot table**, with **no date parameter at
all**. The resulting symbol list is then held as one fixed Python list (`universe`) for the
entire backtest; `_rank_universe(d)` (called once per rebalance date) only ROC-ranks *within*
that fixed set — it never re-resolves which symbols pass the GVM/fundamental filter as of `d`.
**Confirms the card's own suspicion exactly**: one current-date filter pass, applied uniformly
across the whole period. The momentum/ROC/RSI/EMA gates, by contrast, genuinely are
point-in-time — they read `raw_prices` through `_Series.as_of()`, a real as-of (`<=`) bisect
lookup (line 63) — so this bias is specific to the fundamental/GVM entry filter, not the whole
engine.

**Important, precise correction to the card's own framing**: this is not a silently-hidden bug.
The module's own docstring (line 11-14) already names it — *"Fundamental/GVM universe filters
currently resolve against the CURRENT snapshot... so the result is flagged PIT_PARTIAL and the
front end must show that badge."* Checked, not assumed: `pit_partial` is correctly computed
(`pit_flag in ("current_snapshot",)`, line 301) and genuinely wired through to
`scorr_v12.html:262`, which renders an amber `PIT_PARTIAL` pill with an explanatory tooltip
whenever a filtered (non-frozen) universe is used. Frozen universes (`frozen_symbols`, an
explicit founder-picked list) are correctly exact — `pit_flag="frozen"`, no bias, no badge. So:
**the honesty disclosure this card's docstring promised is real and already shipped** — the open
question is whether the underlying limitation can now be fixed, not whether it's being hidden.

## Item 2 — what a genuine point-in-time universe needs, checked against what exists today

A truly point-in-time GVM/fundamental filter needs `gvm_history` (not `gvm_scores`) queried
**as-of each historical rebalance date**, the same `as_of(sym, d)` bisect pattern `_Series`
already uses for prices. Checked what `gvm_history` actually holds right now, immediately after
cc#2091's cleanup (this session, same sitting): **737 distinct symbols, 78,453 rows, spanning
only 2026-05-30 → 2026-09-13** — roughly 3.5 months of real daily/near-daily snapshots. The deep
historical series (2021-07 → 2026-05, ~1.4M rows) that used to sit in `gvm_history` was the
`backfill_step_partial` method — G and V frozen to today's value on every past date (cc#2090's
diagnosis, confirmed and deleted this session as part of cc#2091's cleanup). **There is currently
no genuine point-in-time G/V score anywhere for any date before 30-May-2026.**

Building that deep historical series is **exactly cc#2092's job** (GVM HISTORY BACKFILL,
annual-stepped, point-in-time peer averages) — already investigated this session (see
`cc_task_logs` task_id=2092): the backfill itself is real, bounded engineering work, not a
months-long data-collection wait, but it is **not yet built**. `fundamentals_history` (the raw
input cc#2092 rebuilds from) is itself now correctly scoped and real (11 years of annual
line-items per symbol for names like RELIANCE, checked directly) — the gap is specifically that
nobody has yet run the point-in-time G/V *compute* over that raw data and written it to
`gvm_history` under a new, honest method tag.

## Item 3 — cost/feasibility, stated plainly

**This card's own fix (item 4, a genuinely dynamic point-in-time universe mode) is blocked on
cc#2092, not on new data collection.** Once cc#2092 lands deep historical `gvm_history`, the
code change here is well-defined and moderate in size: extend `_resolve_universe()` to accept a
target date, add a `gvm_history`-as-of lookup (mirroring `_Series.as_of()`'s own bisect pattern)
for a filtered universe, and call it once per rebalance date inside `_rank_universe(d)`'s loop
instead of once before the walk starts. Frozen and manual universes need no change — they are
already exact. This is real, boundable, moderate work once its one dependency is met; it is not
a case of "insufficient history that has to run for months" — the raw fundamentals exist today,
the compute over them does not yet.

## Item 4 (per the card's own instruction) — keep BOTH modes once built

Confirmed the existing frozen-universe path already satisfies half of this requirement — a
frozen `v12_universes` definition is untouched by any of this and stays exact today. The locked
(current-snapshot) mode for filtered universes should stay available too, once a dynamic mode
exists, exactly as item 4 asks — this needs no design work beyond what already exists; the
`pit_partial` badge itself is the natural place to also offer "run locked" vs "run dynamic" as an
explicit choice once both are real, a small addition when the time comes.

## Item 5 — do not build yet, correctly not done here

Per the card's own explicit instruction: *"Do NOT build the fix yet if item 3 reveals this needs
new historical data collection that has to run for months before it is usable — report that
honestly rather than shipping a fix with insufficient history."* Item 3's honest finding is a
**real but bounded dependency on cc#2092**, not a months-long wait — reported plainly, fix not
built here, matching the card's own gate exactly.

## Verify

Every claim above is a direct code citation (`v12_backtest.py` line numbers, `scorr_v12.html`
line number) or a real query against production Postgres run this same session — nothing is
inferred or assumed. `gvm_history`'s current post-cleanup shape (737 symbols / 78,453 rows /
2026-05-30 to 2026-09-13, zero deep history) is the same real state left by cc#2091, not a
separate claim.

**Recommendation**: complete cc#2092 first (already scoped, next candidate in the queue once
given dedicated implementation time), then return to this card's item 4 as a moderate, well-
defined follow-up. No code changes ship with this report — correctly, per the card's own gate.
