# cc#2093 — V12 backtest: trailing_peak_pct / rank_fall_y exits now enforced

## Founder decision (found in the spec's own `founder_decision` key, delegated via Fable)

> (1) YES, force-close a symbol on trailing_peak_pct/rank_fall_y breach even while still
> top-X ranked -- same as cc#2088's atr_stop. (2) Freed slot goes to CASH until the next
> rebalance -- same as cc#2088's atr_stop choice, for consistency. Do not backfill from the
> next-best-ranked candidate.

## The bug, confirmed exactly as cited

`v12_backtest.py _do_rebalance()` computed a `keep` dict from the trailing_peak_pct/rank_fall_y
breach checks, but `keep` was never referenced again — `target`/`new_holdings` came purely from
`scored[:top_x]` (the ROC ranking). A holding only ever left because it fell out of the top-X
ranking, never because it specifically breached its trailing stop or the rank-fall threshold.
cc#2088's own comment already flagged this as a known, deliberately-deferred gap.

## The fix

`_do_rebalance()` now tracks *why* a held symbol would drop (`forced_exit_reason`, distinguishing
`rank_fall_y` from `trailing_peak_pct` — rank_fall_y checked first, matching the existing
evaluation order, first-hit wins the label on the rare cycle both breach at once). After
`new_holdings` is built from ranking, every symbol in `forced_exit_reason` is force-popped —
**even if the ranking would have kept it** — with its `exit_reason` recorded in the trade log
(recorded unconditionally, not just when still in `new_holdings`, so a symbol that both fell out
of ranking *and* breached isn't mislabeled "rotation").

This is the exact same hard-override pattern cc#2088 already uses for `atr_stop`, applied at the
same point in the function, writing into the same shared `exit_reason` dict. **Freed slots are
not rescaled or backfilled** — `new_holdings`'s other weights keep their original
`tw = 1/len(target)`, so the vacated share stays uninvested until the next rebalance, per the
founder's explicit choice.

## Scope item 4 — "show the stats difference," honestly

Checked before promising a clean before/after: there is **no manual trigger** for this file's own
gated self-test (`v12_bt_selftest`, cc#2087) — only a boot-time `@router.on_event("startup")`
check — and **no real historical run exists to diff against anyway**: `v12_baskets` and
`v12_backtests` are both confirmed empty (established during this card's own earlier discovery).
Re-simulating the *entire* engine twice (pre-fix vs post-fix) against real data from this sandbox
would mean stubbing `run_backtest()`'s full cursor surface — GVM screening, multi-year price
series for the whole universe — a different scale of effort than the targeted stub-cursor harness
this session uses for smaller modules, and not something this sandbox's lack of a live DB
connection makes cheap to get right.

**What this report does instead, and will complete on confirmation:** the fix is pushed with the
existing `v12_bt_selftest` self-test re-armed (same basket cc#2087 already uses:
`exit.trailing_peak_pct: 15`, GVM≥7 universe, monthly, 5-year window) so the next deploy boot
produces **real first-run evidence with the fix active** — confirming forced exits actually fire
(trade rows with `exit_reason` in `trailing_peak_pct`/`rank_fall_y`, not just `rotation`), which is
the direct, load-bearing proof the dead code is no longer dead. For the *stats impact*
specifically: once real forced-exit trades exist in that run, the concrete counterfactual (what
that symbol's price did afterward, had the old code kept holding it) will be pulled from real
`raw_prices` rows and added here as a follow-up, rather than asserted without the artifact.

## What did NOT change

`exit.atr_stop` (cc#2088) — untouched, its own separate code path. The turnover-cost calculation,
trade-open bookkeeping, and every other part of `_do_rebalance()` are unchanged; this fix only
adds the missing enforcement step and its labeling.
