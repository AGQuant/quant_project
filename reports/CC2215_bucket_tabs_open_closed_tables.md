# cc#2215 -- /v8 four bucket tabs: separate OPEN and CLOSED tables

## What changed

Each of the four bucket tabs (Buy Reversal, Buy Momentum, Sell Reversal, Sell Momentum) now
renders three separately-headed tables, in this order:

1. **Qualified today, entry gated** -- the existing table, unchanged in columns/legend, but now
   filtered so a name already OPEN elsewhere on the same side never double-counts as a fresh
   signal (see Suppression below).
2. **Open** -- SYMBOL / SIDE / ENTRY TIME / ENTRY PRICE / CMP / DAYS / NET P&L / NET P&L %, sourced
   from `v8_paper_positions` for this basket. Renders an explicit empty state, never hidden
   (`sell_reversal` and `buy_momentum` currently have zero open).
3. **Closed** -- SYMBOL / SIDE / ENTRY TIME / ENTRY PRICE / EXIT TIME / EXIT PRICE / EXIT REASON /
   NET P&L / NET P&L %, sourced from `v8_paper_trades`, era-bounded, newest-exit-first, 20-row
   chunks with a "Show more" reveal. Prints the era caption (`v8_era.era_block()`), never typed in.

No row is ever merged or duplicated across the three tables.

## Suppression (the founder's decisions on the card)

The qualified table's "first table = valid signals only" rule: a symbol already OPEN on a given
side, in EITHER of that side's two buckets, is suppressed from BOTH buckets' fresh-signal list --
scoped to `(symbol, side)`, never `(symbol, basket)`. It still renders correctly as an OPEN row on
whichever basket actually holds the position. This is display-only: the engine's own gate order
(`has_open` / `cooloff` / ...) in `v8_paper_missed` is untouched.

Live case as filed on the card (18-Sep): LTM and MPHASIS are OPEN SHORT in `sell_momentum` --
`sell_reversal`'s qualified table drops from 6 rows to 4 (INFY, HCLTECH, KPITTECH, TATAELXSI); LTM
and MPHASIS render only in `sell_momentum`'s own OPEN table.

## Files changed

- **v8_endpoints.py** -- `qualified()`:
  - `_cc2215_side_family(basket)` -- pure `(side, (basket1, basket2))` pairing.
  - `_open_symbols_same_side(basket)` -- the suppression set query, scoped to exactly the two
    baskets in that side family (never the legacy `buy_s1_bounce`, which has no tab).
  - `_cc2215_open_row` / `_cc2215_open_table` -- built from `open_pos`, the SAME dict
    `_enrich_with_status` already uses for this basket, so the OPEN table can never disagree with
    the qualified table's own OPEN rows.
  - `_cc2215_closed_row` / `_cc2215_closed_table` -- one query against `v8_paper_trades`, bounded
    by `entry_ts >= app_config.v8_paper_rebuild_cutover_ts` (the same era bound V8_PNL_CANON_V1 /
    V8_ERA_CUTOVER_ONLY_V1 already use), capped at 200 rows.
  - Response gains `open_table`, `closed_table`, `era_block`.
  - `retired_baskets` import line now also pulls `BROKERAGE_PER_TRADE` from `v8_book_canon` (one
    constant, not a second copy).
- **v8_dashboard.html** -- `renderFunnel(b)`:
  - New `cc2215OpenClosedBlock(b,d)` renders both new tables (desktop `sortTbl` + mobile cards) and
    is appended on BOTH of `renderFunnel`'s return paths, including the empty-qualified early
    return, so OPEN/CLOSED still render on a day a bucket fires nothing.
  - `cc2215ClosedMore(bid)` -- the show-more reveal, same shape as the existing `newsShowMore()`.
- **tests/test_cc2215_open_closed_tables.py** -- 10 tests, DB-free, on the pure row/pairing
  functions.

## NET P&L -- which convention, and why

Two different, both already-established conventions on this exact page, kept exactly as they are:

- **OPEN**: raw unrealised P&L, no brokerage deducted. V8_PNL_CANON_V1 (rule 13) only nets
  brokerage on REALISED P&L; an open position hasn't paid an exit leg yet, and `book_canon.py`'s
  own unrealised formula is never fee-adjusted either.
- **CLOSED**: `pnl` minus ROUND-TRIP brokerage (`BROKERAGE_PER_TRADE * 2` = Rs.1000), matching
  `v8_dashboard.html`'s own `renderTradeLog` per-row "Net P&L" column exactly (`x.pnl - BROKERAGE_PER_TRADE*2`,
  not `*1`). NET P&L % is `net_pnl / (entry_price * qty) * 100` -- the same notional base
  `return_pct` uses, just over the net rupee figure instead of gross.

DAYS on the OPEN table is deliberately NOT computed server-side -- the frontend already has
`holdDays(entry_ts)` ("Day 1 = entered today", cc_task #76 4b) used elsewhere on this page, and
this table reads that one existing convention rather than growing a second day-count formula.

## Validation done in this sandbox

- `ast.parse` on `v8_endpoints.py` after every edit.
- `node --check` on all 8 inline `<script>` blocks extracted from `v8_dashboard.html`.
- `python3 -c "import v8_endpoints"` -- imports cleanly, no DB needed at import time.
- `pytest tests/` -- 115 passed, 10 skipped (pre-existing, DB-gated), 0 failures, including the 10
  new cc#2215 tests.
- Read-only: confirmed current live counts match the card's own evidence (`buy_reversal` 1 open /
  `sell_momentum` 9 open, others 0; post-cutover closed counts 26/65/60/29 across the four baskets).

## What could not be verified from this sandbox

No `DATABASE_URL` here, so the actual `/api/v8/qualified/{basket}` response (with `open_table` /
`closed_table` / `era_block` populated) and the rendered `/v8` page on all four tabs are the
founder's / Fable's live check -- same constraint noted on every other card this session.

## Verify checklist (from the card's own spec)

- [ ] Open each of the four bucket tabs, confirm three separately-headed tables, no shared rows.
- [ ] DB spot-check per tab: OPEN row count == `v8_paper_positions` count for that basket/OPEN;
      CLOSED row count == post-cutover `v8_paper_trades` count for that basket.
- [ ] `sell_reversal` and `buy_momentum` render a visible empty OPEN section, not a collapsed one.
- [ ] `buy_s1_bounce` rows appear on none of the four tabs.
- [ ] Founder screenshot of all four tabs (Claude-web cannot browse scorr.in).
