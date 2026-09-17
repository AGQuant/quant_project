# cc#2176 — /m/tcscan hero → swipeable rail of three cards: Long unrealised, Short unrealised, Net — each with its accuracy inside

Founder review 17-Sep 12:23 IST: "make the top swipeable like the Net P&L card you already built — three cards: Long unrealised, Short unrealised, Net; keep unrealised as the default; inside each card mention accuracy too." Built 17-Sep-2026 from 14:18 IST server time (the Started line); the checks below ran up to 14:26 IST server time. The landing time is in the task row.

## What shipped

| File | Change |
|---|---|
| `scanners_app_mobile.py` | Additive. **`open.by_side`** = `{BUY: {count, priced, rows_without_lot, rows_without_cmp, unrealised_rs, unrealised_pts_pct}, SELL: {…}}` from `_by_side()`: the SAME per-row `pnl_rs` / `pnl_pct` that `tc_scanner_holds()` already computed ((mark − entry) × lot, sign-aware), summed per side over the open rows; rows without a lot or a cmp are counted, not summed — the one-lot rule the page prints. `open_all_book` carries no per-side split, so this sums the very rows it sums; `open.by_side_basis` says so in the payload, and BUY + SELL = `open.book.unrealised_rs_one_lot`. A side with nothing open is `count 0` and `None` figures, never 0.00. **`record.ALL`** comes from the SAME `_record` query with `GROUP BY ROLLUP (h.side)` (the ALL row is `GROUPING(h.side) = 1`): the database adds both sides up over the same closed rows, nothing is summed a second time in Python, so BUY + SELL and ALL cannot drift. `record.BUY` / `record.SELL` are byte-for-byte what they were. |
| `mobile/tcscan.html` | The hero is a **header line** ("TC scanner · paper book · one lot per signal" / "**7** open now · unrealised **−₹2,957** · 7 of 7 priced") plus a **snap rail of three cards** (flex 0 0 86vw, max 360 px, `scroll-snap-type: x mandatory`, a dot pager driven by a plain scroll listener, no touch handlers — the home / V8 deck pattern). Card 1 **Long · unrealised** (the rail starts here): headline = `by_side.BUY.unrealised_rs` signed in win/loss colour, sub "N open · x of N priced · +y.y pts", accuracy block "Record 14.3% · 1 of 7 wins" with the existing bar from `record.BUY`. Card 2 **Short · unrealised**: the same from SELL. Card 3 **Net · 1 lot · realised**: `record.ALL.net_rs_one_lot` (+₹43,980), sub "+15.9 pts · 36 signals · since 07 Sep", accuracy "Record 44.4% · 16 of 36 wins" with the bar. Every figure is the payload's; a missing one is a dash with its reason ("Nothing open on the long side", "no lot size on record", "no price on record", "nothing closed yet"), never 0. The old three-cell strip, its `.kg/.cell` CSS, the `.hero` CSS and `statCell()` are gone. Page-local `tk-` class names throughout, modifiers included (`tk-long`, `tk-short`, `tk-net`). BOOK toggle, OPEN list, sort chips, closed list, the rule card: untouched. One line inside the header: an unpriced book shows a dash for unrealised (upstream `open_all_book` sums an empty priced list to 0.0; the page no longer prints that ₹0). |
| `tests/test_tcscan_by_side.py` | New, 4 tests: by_side sums the same per-row rupee and counts the rest (BUY + SELL == the book total); a side with nothing open is None, never 0; record.ALL comes from the rolled-up query (SQL carries `GROUP BY ROLLUP (h.side)` + `GROUPING(h.side)`, ALL = 16 of 36 = 44.4%); the old keys are untouched. |
| `reports/CC2176_tcscan_hero_rail.md` | This report. |

## Production (spec verify item 1), 17-Sep 14:20–14:26 IST server time

**The rolled-up `_record` query, run on production as the endpoint now runs it:**

| side | closed | wins | wr | net pts | since | last | rs_rows | net ₹ one lot |
|---|---|---|---|---|---|---|---|---|
| BUY | 7 | 1 | 14.3% | −13.78 | 07 Sep | 15 Sep | 7 | −1,02,222.75 |
| SELL | 29 | 15 | 51.7% | +29.68 | 07 Sep | 17 Sep | 29 | +1,46,203.00 |
| **ALL** | **36** | **16** | **44.4%** | **+15.89** | 07 Sep | 17 Sep | 36 | **+43,980.25** |

Identical to the card's evidence (ALL = 16 of 36 = 44.4%, +₹43,980, +15.9 pts). BUY + SELL = −102,222.75 + 146,203.00 = 43,980.25 = ALL.

**The open book at 14:20 IST**: 7 open, all SELL (TCS, ASIANPAINT, IRFC, BANKINDIA, 360ONE, RELIANCE, IEX), all 7 with a lot and a cmp. Per-row one-lot rupees at that tick: −1,957.50 · −10,775.00 · +1,356.25 · −4,628.00 · +2,750.00 · +7,600.00 · +2,697.00 → **SELL unrealised −₹2,957.25**, BUY nothing open → header −₹2,957 (the founder's −₹7,945 at 12:23 has moved with cmp). So today the live rail opens on a Long card that reads "— · Nothing open on the long side · Record 14.3% · 1 of 7 wins", then Short −₹2,957 · 7 open · 7 of 7 priced · −0.3 pts · Record 51.7% · 15 of 29 wins, then Net +₹43,980.

## Harness (Playwright, real Chromium, `scratchpad/cc2176_test.py`) — 76 checks, ALL PASS at 375×812 goldnight + aquawhite and 390×844 goldnight

Fixture = the open book and record above in the endpoint's own shape, plus two variants: a mixed book (two long open, one without a cmp) and a one-row book with no lot and nothing closed.

- Three cards in order Long · unrealised / Short · unrealised / Net · 1 lot · realised; the rail opens on Long with the first dot on; card widths 323 px at 375 and 335 px at 390 (= 86vw); the page never overflows sideways (scrollWidth = viewport).
- Header keeps the totals: "7 open now · unrealised −₹2,957 · 7 of 7 priced".
- **Numbers on the cards = the payload fields, side by side**: Long → "—" / "Nothing open on the long side" (by_side.BUY.count 0); Long accuracy "Record 14.3% · 1 of 7 wins", bar 14.3% (record.BUY); Short → "−₹2,957" / "7 open · 7 of 7 priced · −0.3 pts" (by_side.SELL −2,957.25 / −0.25 pts); Short accuracy "Record 51.7% · 15 of 29 wins", bar 51.7% (record.SELL); Net → "+₹43,980" / "+15.9 pts · 36 signals · since 07 Sep", accuracy "Record 44.4% · 16 of 36 wins", bar 44.4% (record.ALL).
- Swipe: scrolling the rail to card 2 turns the second dot on; tapping the third dot scrolls to Net (scrollLeft = max) and turns the third dot on; a BOOK-toggle redraw keeps the rail on Net.
- Mixed book: Long "+₹7,525" / "2 open · 1 of 2 priced · +0.9 pts" in win colour; header "9 open now · unrealised +₹4,568 · 8 of 9 priced · 1 without a price"; BUY + SELL (7,525.00 + −2,957.25) == the book total 4,567.75.
- No-lot book: Long "—" / "1 open · 0 of 1 priced · no lot size on record" / "Record — · nothing closed yet"; Net "—" / "Nothing closed yet"; header "1 open now · unrealised — · 0 of 1 priced".
- Every card stacks eyebrow → number → accuracy top to bottom at one height (140 px), `display: block`. The old `.kg/.cell/.hero` nodes: 0. BOOK toggle "All", 8 position rows (7 open + 1 closed) untouched. No page errors on any theme.

**Caught by the harness, fixed before push:** the first run rendered the Net card as a row — its modifier class was the bare word `net`, and the shared `mobile_app.css` has bare `.net .k` / `.net .v` rules (the cc#2155/2156 leak). All three modifiers are now `tk-long` / `tk-short` / `tk-net`; the harness asserts the stacking on every card.

**Screenshots looked at** (`scratchpad/cc2176_*.png`, 13 files): `375_dark_live_long` — the header line, then the Long card with the green edge stripe, a dash, "Nothing open on the long side", "Record 14.3% · 1 of 7 wins" with a short green bar, the Short card peeking at the right edge, the gold first dot; the BOOK toggle, Open 7 and the sort chips below, unchanged. `375_dark_live_short` — the Short card with the red edge stripe, "−₹2,957" in red, "7 open · 7 of 7 priced · −0.3 pts", "Record 51.7% · 15 of 29 wins" with the red bar half across, second dot on. `375_dark_live_net` and `375_light_live_net` — the Net card with the gold edge, "+₹43,980" in green, "+15.9 pts · 36 signals · since 07 Sep", "Record 44.4% · 16 of 36 wins" with the gold bar, third dot on; the light theme the same in aqua. `375_dark_mixed_long` — "+₹7,525" in green with "2 open · 1 of 2 priced · +0.9 pts". `375_dark_nolot` — the header "unrealised —", the Long card's two dash lines with their reasons. `375_light_live_long`, `390_dark_live_net` — the same layout on the light theme and at 390.

Observation, not this card's: the open rows below the rail show the CMP / ENTRY label collision on RELIANCE, IEX and 360ONE in these screenshots — that is cc#2177, next in the queue; nothing in the rows was touched here.

## Checks

- `ast.parse` on `scanners_app_mobile.py`: OK. `node --check` on both inline scripts: OK. Literal `var(--x, #hex)` fallbacks in `mobile/tcscan.html`: 0 (grep); `theme_validate` runs on the deployed file — its result is stated in the task row after landing.
- `pytest tests`: 59 passed, 10 skipped (the 4 new tests included).
- No NAV change (same route `/m/tcscan`).

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/tcscan` → `open.by_side.BUY` / `.SELL` present with `unrealised_rs`; `open.by_side.BUY.unrealised_rs + open.by_side.SELL.unrealised_rs == open.book.unrealised_rs_one_lot` (None counts as 0); `record.ALL.closed == record.BUY.closed + record.SELL.closed` and `record.ALL.net_rs_one_lot == the two sides' sum`.
2. `https://scorr.in/m/tcscan` on a phone: header line, then the rail on Long; swipe → Short → Net; each card carries its Record line; the three numbers equal the payload fields above; no sideways page scroll.

## Out of scope (by spec)

What counts as a win, the one-lot rule, the position rows (cc#2177), `tc_scanner_endpoints` (read-only).
