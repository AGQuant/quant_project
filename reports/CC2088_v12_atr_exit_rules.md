# cc#2088 (normal) — V12 EXIT RULES: ATR-based stop/target

Built under explicit founder authorization (14-Sep ~19:44 IST, "fable not available, limit
finished and clear the queue push all to main, founder decision") — Fable is unavailable, the
founder is directing CC to build the remaining engine/backend queue directly for this reason.

Reference: `docs/ROBO_BASKET_FILTER_REGISTER_v1_1.md` section A4 item 4 — ATR(14) weekly, stop and
target as ATR multiples (fixed or trailing), because a flat-percent stop is too tight for a
volatile smallcap and too loose for a steady largecap.

## Item 1 — checked for an existing weekly ATR before building one (per the card's own instruction)

Found three existing ATR implementations, none reusable as-is:
- `v10_st_ema._atr()` — the right FORMULA (Wilder-smoothed True Range) but on 5-min intraday bars,
  NIFTY/BANKNIFTY only, live-tick-driven — the wrong data source per this batch's EOD-basis ruling
  and not point-in-time-queryable for an arbitrary historical date.
- `deriv_metrics._atr_daily()` — daily and EOD-sourced (`raw_prices`, right table), but "latest N
  rows as of NOW" only (`ORDER BY price_date DESC LIMIT period+1`), not as-of any historical date,
  and a simple mean of true ranges rather than Wilder-smoothed.
- `v14_engine._atr` — located, not point-in-time-capable either.

None compute a point-in-time WEEKLY series. Built `_load_weekly_atr()` new in `v12_backtest.py`,
reusing `v10_st_ema._atr()`'s exact Wilder formula **by value, not by import** — that file is a
live intraday engine, this is a backtest module, and a backtest-to-live-engine import for one
numeric formula is an unnecessary coupling across an architectural boundary.

**EOD-basis compliance (14-Sep founder ruling, stated explicitly per that ruling's own
instruction):** `_load_weekly_atr()` reads only `raw_prices` (`high`, `low`, `close`) — the same
EOD daily-bar table the rest of `v12_backtest.py` already uses for close-price point-in-time
lookups. No intraday or live source (`intraday_prices`, `cmp_prices`, `v10_st_ema` live ticks) is
touched anywhere in this card.

**Point-in-time design**: weeks are ISO calendar-week buckets (Mon-Sun) built from each symbol's
own trading days in `raw_prices` — a holiday just shrinks a week's bucket, never shifts it. Each
week's Wilder ATR value is keyed at that week's own LAST trading date and returned as a `_Series`
(the same generic point-in-time container the file already uses for close prices), so
`as_of(sym, d)` can only ever see a week that is fully known by the close of day `d` — a week
still in progress never leaks its still-forming high/low into a lookup that lands mid-week.
Confirmed for real (not just reasoned about), see Verify below. Symbols with fewer than 15 weekly
bars get no series at all — an honest `None` on lookup, never a fabricated value, matching the
file's existing "insufficient history -> None" convention for `_roc()`/`_rsi()`/`_ema()`.

## Item 2 — additive, trailing_peak_pct genuinely untouched

`exit.trailing_peak_pct` and `exit.rank_fall_y`'s own code path is byte-identical to before this
card — not a single line inside that `keep`-building loop changed. `exit.atr_stop` is a new,
separate key; a basket that never sets it never triggers `_load_weekly_atr()` at all (the call is
itself gated: `atr_series = _load_weekly_atr(...) if atr_stop else None`), so there is no added DB
query, no added computation, and no behavior change for any basket not using the new option.

`_validate_basket_def()` (`v12_endpoints.py`) gets a new, equally optional `exit.atr_stop` block
— `{mult: positive number (required if atr_stop is present), target_mult?: positive number,
trailing?: bool}` — matching the file's own existing style exactly (`if "k" in x and not
_isnum(...)`, same as `weight_max_pct`/`weight_cushion` immediately above it).

## Item 3 — UI wiring: a correction to the card's own file reference, checked before building

The card names `quant_basket.html` Step 2 (Exit). Grepped it first (Role Split: "data-source gates
... CC greps and answers before the build proceeds") — **that file has no Step 2, no Exit panel,
and no `trailing_peak_pct` at all.** It is the QB page (the six fixed algorithmic baskets,
monitoring/cards view) — it links OUT to `/v12` (line 251: `Basket Builder →`) rather than
containing the builder itself. The actual V12 Basket Builder wizard, with the real Step 3 Exit
panel and the existing `x_trail`/`x_rank` trailing-stop controls the card is asking to sit
alongside, is **`scorr_v12.html`**. Built there instead — same page family, correct file.

Added: an "ATR stop (weekly ATR-14)" checkbox in the Step 3 Exit panel, revealing a stop-multiple
field, an optional target-multiple field, and a trailing checkbox — same `.fld`/`.chk` markup and
same show/hide-on-toggle pattern already used for the RSI/EMA gate boxes in Step 2. Wired into
`buildDefinition()` (emits `exit.atr_stop` only when the checkbox is on and a multiple is set) and
`applyPreset()` (restores the fields when loading a saved basket/preset that used it) alongside
the existing `x_trail`/`x_rank` handling, unchanged. Also added an "Exit reason" column to the
trade-log table in `renderResults()`, so `atr_stop` / `atr_target` / `rank_fall` / `rotation` is
visible on the page itself, not just in the raw JSON — a stop-loss feature that never shows why a
trade closed would be half-built.

## Item 4 — wired into v12_backtest.py so it actually exits, not just validates

`run_backtest()` pre-loads a point-in-time weekly-ATR `_Series` for the whole universe once, in
the same `with _conn()` block as the existing close-price series (only when `atr_stop` is set).
`_do_rebalance()` freezes each position's entry-date ATR into `open_pos[sym]["entry_atr"]` when it
opens — the distance basis stays fixed for the life of the position, whether the stop is fixed or
trailing, per the reference doc's "either fixed or trailing" framing. Each rebalance, a position
still inside the ranked target list is force-closed if `close <= entry_px - mult*entry_atr` (fixed)
or `close <= peak_since_entry - mult*entry_atr` (trailing), or force-closed at target if
`close >= entry_px + target_mult*entry_atr` (target is always entry-anchored, not trailing — a
take-profit has no trailing analogue in this design). A stopped-out slot goes to cash (no
reweight/backfill) until the next rebalance re-ranks fresh — the simplest, most literal reading of
"stopped out," and it required no change to the existing turnover-cost math (already weight-delta
generic).

**A discovered pre-existing bug, found while wiring this in, reported rather than silently fixed
or silently left undocumented:** `_do_rebalance()`'s exit-check loop for `trailing_peak_pct` and
`rank_fall_y` computes a `keep` dict (which holding survives the trail/rank-fall check) — but
`keep` is **never referenced again**. The actual holdings for the next period come purely from
`scored[:top_x]` (the ROC-ranked list), so a holding only ever leaves because it fell out of the
top-X ranking, never because it triggered `trailing_peak_pct` or `rank_fall_y` specifically — both
of those exit types have been computed-but-inert since this file was first built. This is why
`atr_stop` needed its **own**, separate enforcement path (applied to `new_holdings` directly, not
folded into the dead `keep` pattern) rather than following the existing code's own template —
copying that template would have made `atr_stop` silently inert too, which would have failed this
card's own verify clause. Per this card's own item 2 instruction — "keep exit.trailing_peak_pct
exactly as it is for baskets already using it" — that dead path was **not** touched or fixed here;
it is a real, separately-scoped bug affecting two already-shipped exit types with its own
behavior-changing fix, not a one-line cleanup, so it is filed as its own cc_task rather than
patched inline (see below).

## Verify

**Syntax**: `python3 -m py_compile` clean on `v12_backtest.py` and `v12_endpoints.py`; `node
--check` clean on `scorr_v12.html`'s inline script.

**Real-data test against the actual, just-edited `v12_backtest.py`** (not a re-typed copy — the
module was imported directly and `_load_weekly_atr()` called through a stub cursor serving real
SUZLON `raw_prices` rows fetched live from production Postgres, 2021-06-01 to 2023-06-30, 518
rows):
- Produced a 95-point weekly ATR series, all values positive and in a plausible range for a
  Rs 5-16 stock (max observed 1.33 vs a max close of 15.65).
- **Point-in-time confirmed directly, not assumed**: `as_of('SUZLON', 2023-06-07)` (a Wednesday
  inside a week that had a huge single-day range on that same day, high 14.60 vs the week's Monday
  open near 11.50) returned 1.0198 — the PRIOR completed week's ATR — while `as_of('SUZLON',
  2023-06-09)` (that week's own last trading day) returned 1.2041, reflecting the just-completed
  week's real range. The mid-week lookup does not see its own still-forming week — no look-ahead.
- **A real historical stop-trigger, using the exact `_do_rebalance` formula**: a simulated entry
  on 2022-04-20 (close 9.87, the local peak before SUZLON's ~35% mid-2022 decline) with weekly ATR
  frozen at entry (1.1442) and `mult=2` gives a stop level of 7.5815 — the real closing-price
  series crosses below that level on 2022-05-26 (close 7.21). Confirms `exit.atr_stop` is not just
  syntactically present but functionally reachable against real market behavior at a realistic
  multiple.

**Unaffected claim, checked not assumed**: every new code path in `run_backtest()`/`_do_rebalance`
is gated behind `if atr_stop` / `if atr_stop:` / `atr_series is not None` — a basket definition
with no `exit.atr_stop` key takes none of the new branches, runs `_load_weekly_atr()` zero times,
and its `trailing_peak_pct`/`rank_fall_y` code is byte-for-byte what it was before this card.

**Still to run once deployed** (rule 15 — a push is not done until on `main` and deployed): a live
`/api/v12/backtest` call with a real `exit.atr_stop` definition against the actual deployed
endpoint, polled to completion, checked for at least one `exit_reason: "atr_stop"` trade in the
real result — the literal end-to-end form of this card's own verify clause. Will run and log the
result once the push lands and Railway's ~90s auto-deploy completes.

## Follow-up filed

A new cc_task for the discovered `keep`-dict dead-code bug (trailing_peak_pct/rank_fall_y computed
but never enforced) — a real, separately-scoped, behavior-changing fix affecting two already-
shipped exit types, correctly not touched inside this card per its own "leave trailing_peak_pct
exactly as it is" instruction.
