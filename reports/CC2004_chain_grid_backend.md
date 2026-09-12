# cc#2004 — backend: the shared chain-grid data builder

First push toward cc#2004 (NSE-style option chain grid). Lands the ONE data builder cc#2004,
cc#2003 and cc#2006 all explicitly say they share ("one implementation, one data path, shared
strike range" — cc#2004's own `founder_revision_12sep`). The frontend grid (full borders, tag
dots, coloured full-row highlighting, tap-to-detail panel, (i) explainer, legend) is a separate,
substantial piece of work and follows in its own push — same reasoning as cc#2005's backend/
frontend split.

## What this lands

**New file `option_chain_grid.py`** (rule 5: new feature, own file, `include_router()` in
`main.py`), one new function and one new additive endpoint:

- `build_chain_grid(symbol)` — calls `deriv_metrics.strike_chain(symbol)` **unchanged** (cc#1576/
  cc#1859's own pipeline: already ATM±10 for both stocks and indices, already tag-annotated,
  already the single source cc#1859 built) and adds, only when the symbol is an index
  `oi_structure`/`max_pain` covers: per-strike OI, and the max-pain/call-wall/put-wall flags,
  merged onto `strike_chain()`'s own rows by strike.
- `merge_oi_into_rows(rows, oi_map, max_pain_strike, call_wall, put_wall)` — the actual merge,
  factored out as a **pure function** (no DB access) so it is directly unit-testable.
- `GET /api/deriv/chain-grid/{symbol}` — new, additive. Does not touch `strike_chain()`'s own
  `/api/deriv/strike-chain/{symbol}` route or response shape for any existing caller.

**The index/stock decision reuses `deriv_metrics._INDEX_OPT_ROOT`** — the exact map
`strike_chain()` itself already uses — never `oi_structure.norm_underlying()`, which silently
falls back to `"NIFTY"` for any name it doesn't recognise. Passing a stock symbol to that function
would have silently scored the stock against NIFTY's own chain; this card's own gate stays outside
it entirely, so that trap is never reached for a stock.

## Two real data-source facts, checked directly rather than assumed

- **Stocks get no OI, no wall, no max-pain — confirmed, not new**: `option_chain` (the table both
  `oi_structure` and `max_pain` read) carries index chains only, no stock strikes exist in it at
  all. A stock's grid therefore has `oi_available: false` and every row's `ce`/`pe` carry
  `ltp`/`iv`/`fair`/`tag` (unaffected) with no `oi`/`bid`/`ask` and no wall/max-pain flags. This
  was the open design question from log 6390 — now built exactly that way, stated in the
  module's own docstring so it is overridable in one line if wrong, per the same discipline used
  for cc#2005's gross/net question.
- **Bid/ask is NOT available anywhere, for either stocks or index — checked live, not assumed
  from the schema.** `option_chain` does have `bid`/`ask` columns, and this file's own OI query
  selects them — but a direct count against the live table
  (`SELECT COUNT(bid), COUNT(ask) FROM option_chain`) came back **0 of 59,369 rows** with either
  populated. The columns exist; the feed that fills this table has never written to them. The
  merge still copies whatever is there (harmless, and it starts surfacing real values for free the
  day that feed is extended) but the module's docstring states plainly that today every row's
  bid/ask is `None`, chain-wide — cc#2004 item 7's "bid/ask" field in the detail panel has nothing
  to show yet on either the stock or the index path.
- **OI change** (a separate item-7 field) also is not available: `option_chain` stores a level per
  tick, not a delta, and computing "change" needs a baseline the card does not specify — shipping
  a guessed baseline would be worse than omitting the field, so it's left out and stated as such.

## Graceful degradation

An index whose OI leg is down does not take the whole grid down with it: `build_chain_grid()`
catches an `oi_structure()` failure (or a non-`"ok"` status) and still returns `strike_chain()`'s
own successful ltp/iv/tag rows, with `oi_available: false` and an error/note field — never a hard
500 for a partial outage on the OI leg specifically.

## Verify

- `ast.parse` clean on `option_chain_grid.py` and `main.py`.
- **Real-data SQL check**: the new per-strike OI+bid/ask query (`_oi_and_bidask_map`'s own SQL) run
  directly against production for NIFTY's live chain — returns real strikes and OI values;
  confirmed the bid/ask-always-null finding above from this same check.
- **Isolated Python tests** (`test_option_chain_grid.py`, scratchpad) against the real, committed
  module, with `deriv_metrics.strike_chain`/`oi_structure.oi_structure`/the DB connection stubbed
  at the module-attribute level (the two live composers this file calls, not re-simulated):
  1. `merge_oi_into_rows()` — OI/bid/ask populate correctly per strike; max-pain/call-wall/put-wall
     flags land on exactly the one right strike each; a strike absent from the OI map (should not
     happen inside the shared ±10 window, but tested anyway) gets nulls and no flags, never a
     fabricated zero.
  2. `build_chain_grid()` on a stock (`RELIANCE`) — `is_index=False`, `oi_available=False`,
     `oi_structure()` is **never called** (confirmed via a call counter — proves the stock path
     can't hit the silent-NIFTY-fallback trap), `strike_chain()`'s own rows pass through untouched.
  3. `build_chain_grid()` on an index alias (`"NIFTY 50"`) — resolved via `_INDEX_OPT_ROOT` (not a
     hardcoded literal anywhere in this file), full merge applies correctly end-to-end, hand-
     checked against the synthetic max-pain/wall strikes.
  4. An index whose OI composer raises — degrades to `oi_available=False` with an error note; the
     already-successful ltp/iv/tag rows are not discarded.

## What this push does NOT include

The frontend: the actual NSE-style grid rendering (full inside borders, tag dots, coloured
full-row max-pain/call-wall/put-wall highlighting, tap-a-row-for-an-inline-detail-panel, the (i)
explainer toggle, the bottom legend), wired into the D-button chain first (cc#2004's own primary
target — explicitly not Index Intel, per the card's own item 11). That is a substantial UI build in
its own right and follows in the next push, same split already used for cc#2005. cc#2003 (the Home
popup) and cc#2006 (the Market Mood capsule) both wait on that frontend piece too, since both
explicitly embed the same component this backend now makes buildable. Card not done — Fable
verifies.
