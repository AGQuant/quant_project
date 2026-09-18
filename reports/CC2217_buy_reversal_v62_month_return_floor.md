# cc#2217 -- BUY_REVERSAL V6.1 -> V6.2: month_return floor

## What changed

Exactly one new bound on an existing gate: `month_return` on the `buy_reversal` basket gains a
STRICT floor of 0.0 (`> 0`, not `>= 0`), turning the existing open-ended `< 5` ceiling into a
closed band, `(0, 5]`. `filter_total` stays 9. No other leg, no exit, no regime gate, no other
basket touched.

## Where the real gate lives (and why the registry dict alone was not enough)

`v8_endpoints.py`'s `BASKET_FILTERS["buy_reversal"]` registry dict feeds the i-button, the Strategy
Matrix, `br_stock_passcount` and `br_stock_detail` -- **display only**. The live, money-moving gate
is a separate, hand-written strict-AND in `v8_signal_writer.py`'s
`_write_buy_reversal_v6_qualified` (confirmed via the cc#1179 comment already on that file: *"the
funnel counts come from `_gvm_ok` in this file, which is untouched"*). Editing the registry dict
alone would have changed what the modal SAYS the gate is, without changing what the engine actually
DOES -- so both were updated:

1. **`v8_signal_writer.py`** (the real gate):
   - New `_mr_gt0(s)` helper, same style as the existing `_sw_gt0`/`_d1_gt0` strict-gate helpers --
     `month_return > 0`, NULL fails.
   - `funnel["month_return"]` and the `surv` list comprehension (the actual 9-way strict-AND that
     determines `qualified`) both now require `_mr_gt0(s) AND _passes(month_return, None, 5.0)` --
     the ceiling call is byte-for-byte the same call as before, only the floor is new.
   - `BASKET_FILTERS["buy_reversal"]`'s `month_return` row updated to `min=0.0, cond_min="> 0",
     strict=True` (max/cond_max untouched) -- drives the i-button/passcount/detail surfaces.
   - `BASKET_SPEC["buy_reversal"]` version bumped V6.1 -> V6.2, cc#754 -> cc#2217; the header
     docstring, the function's own docstring and the two in-file V6.1 comments this basket owns are
     updated to match. (Left `v8_endpoints.py`'s own unrelated header-comment mentions of "V6.1"
     untouched -- out of this card's stated scope, which is the registry + engine in
     `v8_signal_writer.py`.)
2. **`v8_endpoints.py`**: no functional change -- `_reg_cond`/`_passes_registry_band` (both already
   existed) automatically pick up the new bound from the registry row.

## A documented, narrow display/engine divergence at exactly month_return = 5.0

`_passes_registry_band` applies the registry row's single `strict` flag to **both** bounds of a
two-sided band when both are present. That is correct for the new floor (which needs to be
strict) but has the side effect of also making the *pre-existing* ceiling strict **on the
display/passcount surfaces only** -- a stock at exactly `month_return == 5.0` would show as a fail
there, while the real engine (whose ceiling call is untouched, still `_passes(v, None, 5.0)`,
inclusive) would still qualify it. `_reg_cond`'s prose ("0 to 5") is unaffected either way -- it
does not consult `strict` when both bounds are set.

This is a genuine, if extremely narrow, side effect of the registry schema having no
`strict_min`/`strict_max` split, not something silently absorbed: it is commented at the registry
row itself, tested explicitly (`test_passes_registry_band_boundary_matrix`), and named here for
Fable/the founder to decide whether a schema follow-up is worth it. No historical `buy_reversal`
trade has ever sat at `month_return == 5.0` exactly (checked against the same 68-trade set below),
so this has never actually fired differently between the two surfaces.

## Backtest re-verified live against Railway (not just trusted from the card)

Re-ran the exact comparison the card's evidence describes, directly against `v8_paper_trades` /
`v8_metrics` (join on `symbol` + `entry_ts::date = score_date`), win/loss classified by **gross
P&L sign** (cc#371's established backtest convention -- distinct from `V8_PNL_CANON_V1`'s
`result='TARGET'/'SL'`-only convention, which is for the live book display, not strategy
backtesting; the two convention differ here because 19 of the 44 proposed trades exited via
`GAP_TARGET_EXIT`/`GAP_SL_EXIT`/`CONFLICT_EXIT`/`GATE_EXIT`, not the plain `TARGET`/`SL` strings):

| Scope | Trades | Wins | Losses | Win % | Avg win | Avg loss | Total return |
|---|---|---|---|---|---|---|---|
| Baseline (no floor) | 68 | 35 | 33 | 51.5% | +2.66% | -3.03% | -6.9% |
| Proposed (month_return > 0) | 44 | 26 | 18 | 59.1% | +2.44% | -2.79% | +13.1% |

Exact match to the card's own stated evidence on every figure above. Also confirmed: exactly one
historical trade (BANKINDIA, entered 04-Sep-2026) sits at `month_return == 0` precisely, and it is
a loser (pnl -25,584, -3.36%) -- independently confirming the card's stated reason for choosing the
strict floor over an inclusive one.

## Validation done in this sandbox

- `ast.parse` on `v8_signal_writer.py` after every edit.
- `python3 -c "import v8_signal_writer"` / `import v8_endpoints` -- both clean, no DB needed at
  import time.
- Direct execution of the registry row, `_reg_cond`, `_passes_registry_band` and a same-logic
  reconstruction of the live engine's floor+ceiling pair, confirmed the intended asymmetric
  boundary behaviour precisely (floor exclusive at 0, ceiling inclusive at 5) -- see the two new
  test files.
- `pytest tests/` -- 121 passed (6 new for this card, 10 for cc#2215 earlier this window), 10
  skipped (pre-existing, DB-gated), 0 failed.
- The 68-trade backtest re-run live against Railway (above), independent of the card's own numbers.

## What could not be verified from this sandbox

The live signal writer only picks this up on its next scheduled tick after deploy; today's market
is already closed (verified: `server_now()` well past 15:30 IST at push time), so first-run
evidence against a LIVE qualification is tomorrow's first tick, not something this sandbox can
produce tonight. Per ENGINE_LIVENESS_RULE, this is stated plainly rather than claimed: **built and
registered, not yet re-confirmed live** -- first-run evidence to follow at tomorrow's market open.

## Verify checklist (from the card's own spec)

- [x] Re-run the 68-trade backtest against the deployed registry -- reproduces 44 trades / 59.1%
      win / +13.1% exactly (above).
- [x] Registry row is STRICT on the minimum, `filter_total` still 9 for the registry-row count
      convention this basket uses.
- [x] `_reg_cond` renders the new band as "0 to 5" for the i-button/passcount/detail (confirmed
      directly; a founder screenshot of the live page is still the final visual check).
- [x] Diffed the whole `BASKET_FILTERS` dict -- no other basket's entry changed.
- [ ] Founder screenshot of the Buy Reversal tab post-deploy (Claude-web cannot browse scorr.in).
