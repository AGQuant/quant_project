# cc#2209 — /m/portfolio report rail in 7 vertical categories; hr_report_pdf.py at section parity

**Card:** cc#2209 P2 UI+PDF (follow-up on cc#2194; session_log 48472 items 1, 2 and 4). cc#2208 (the login redirect) is
separate and landed at fb02442.

## Item 1 — default portfolio + filter icon: verified, no change
`DEFAULT_PID=165` (hr_portfolios id 165 = Akshay Gupta, live DB) and the funnel icon → full-page picker with search were
already in place from cc#2194; the cc#2194 harness (normal 200 path) still passes on this build. Nothing changed.

## Items 2–4 — the app page (`mobile/portfolio.html`)
- **Categorised data, not a restructure.** Every card in `buildCards()` now carries a stable `key`; a `CATEGORIES` array maps
  keys to seven groups, in order: Performance (snapshot, alpha, oneyear, valuation, risk) · Rating (rating, quality) ·
  Composition (sectors, size) · Movers (winners, gainers, losers) · Watch list (upcoming, redflags, replacements, results) ·
  Holdings detail (holdings, breakup) · Takeaways (highlights, expert). Re-grouping later is an edit to that array. A card
  whose key is not listed falls into a trailing "More" group, so a new card can never vanish.
- **Markup.** The page scrolls vertically through the seven `.pf-cat` sections; each has a header (category name + a local
  `n / m` counter), its own dots, and its own `.pf-rail` (the same horizontal snap rail as before) holding only that
  category's cards. The global `1 / 20` counter, the global index chips and the global dots are gone. Each card's eyebrow
  reads `n of m · Category` on the left and keeps the **`web: <section>` attribution unchanged** on the right.
- **Untouched:** every card's content builders (`cells()`, `bar()`, `mixbar()`, `nameRows()`, the 20 `C.push` bodies),
  the snapshot view, the picker, the PDF download flow, `window.pfGo(i)` (now maps a global index to its group and card).

## Item 5–6 — the client PDF (`hr_report_pdf.py`, `render_report_html`)
Six sections added and one row extended, each from a field `build_report()` already returns (the same dict the app's
cards read); placed beside their related sections; the file's own `.sec / .cols / .col / .lbl / .track / .fill / .note /
.muted` classes and colour tokens (#0B6E42 / #B52432 / #9098A8 / #5B667D); no new style. Existing sections' HTML untouched.

| New section | Field read | Placed |
|---|---|---|
| 1yr Nifty 50 / 1yr Nifty 500 row | `benchmark.nifty50_1y`, `benchmark.nifty500_1y` | inside the Valuation & Yield table, under `1yr Port` |
| Risk Metrics (beta, std dev, Sharpe, Sortino, max drawdown, R²; window, risk-free, excluded) | `risk_metrics` | right after the Rating / Valuation columns |
| Company Size | `cap_bands.weights / counts / insight` | two-column block after Sector Allocation |
| Quality Bands | `quality_bands.weights / counts / insight` | same block, right column |
| Winners vs Losers (counts + share bar + insight) | `winners_losers` | just before Top Gainers / Top Losers |
| Rating Breakup (per-holding parameter grid, "+N more" note as on the web) | `rating_breakup`, `holdings` | after Holdings — Full Detail |
| Upcoming Results (symbol, date, when, event) | `upcoming` | after Results This Quarter / Red Flags |

Empty inputs print the honest line ("Risk metrics unavailable — not enough price history.", "No results scheduled for
these holdings.", "No rating breakup on record…"), never a blank block.

## Evidence
- **App harness** (`scratchpad/cc2209_app_test.py`, Playwright 375 × 812, dark + light, the cc#2194 report fixture):
  seven categories in the ruled order with the ruled cards (5·2·2·3·4·2·2 = 20); every category shows `1 / m` and m dots;
  every eyebrow reads `n of m · Category`; all 20 `web:` attributions present once and unchanged; a swipe of two cards inside
  Performance moves only its counter (`3 / 5`) and dots, Rating stays `1 / 2`; `pfGo(6)` lands on Rating · card 2 and scrolls
  the group into view; no legacy `#pf-idx` / `#pf-rail`; no horizontal overflow; no page errors. Screenshots looked at:
  `cc2209_375_dark_top.png` (Performance at card 3 of 5, Rating below), `cc2209_375_dark_movers.png`, `cc2209_375_light_top.png`.
- **PDF harness** (`scratchpad/cc2209_pdf_test.py`): `render_report_html()` on the same payload → the section order is
  Rating Parameters · Valuation & Yield · Risk Metrics · Sector Allocation · Company Size · Quality Bands · Winners vs Losers ·
  Top Gainers · Top Losers · Holdings — Full Detail · Rating Breakup · Results This Quarter · Red Flags · Upcoming Results ·
  Replacement Ideas · Key Highlights · Expert Take · Annexure; the fixture's own numbers appear (beta 1.12, Sharpe 0.61,
  12 : 13 winners/losers, 25 breakup rows, Nifty 50 +5.2% / Nifty 500 +6.8%). Rendered in Chromium at 820 px and looked at:
  `cc2209_pdf_risk.png`, `cc2209_pdf_breakup.png`, `cc2209_pdf_full.png`. WeasyPrint itself was not run in the sandbox
  (the ledger/XIRR helper was stubbed — it has no DB here); `pdf_selftest` on the box is the founder's/Fable's check.
- **Live check:** the sandbox cannot reach scorr.in. Founder: `/m/portfolio?pid=165&view=report` once, and one PDF download.

## Known behaviour
A rail is as tall as its tallest card, so a short card (Winners vs losers) leaves space under it inside its own group —
the same rule the single rail had, now per group (less slack overall).
