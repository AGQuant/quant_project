# cc#2130 — TC Scanner P&L display + position-card clipping; peers sheet contrast + D/W/M table

## Step 0 — where each item lives (grep, not assumption)
| Item | File · function | Line (before) |
|---|---|---|
| 1 Net pts card | `mobile/tcscan.html` `draw()` → `statCell('Net pts', …)`; value from `record.BUY/SELL.net_pts_pct` (`scanners_app_mobile._record`) | 248, 261 |
| 2 clipped `(+₹…/lot)` | `mobile/tcscan.html` `positionRow()` → `.scorr-prow .v .n` (pct) + `.v .c` (rupee) | 220; CSS 78-84 |
| 3 washed-out header label | `scorr_chart_card.js` `_paintChrome()` → `#scorrChartTitle.style.color = p.txt` (a JS palette guess, not a theme token) | 591 |
| 4 Peers D/W/M | `scorr_chart_card.js` `_renderPeers()`; data `chart_peers.build_peers` keys `day_pct/week_pct/month_pct` | 1532-1660 |
| 5 polish | same `_renderPeers()` | — |

Surface B is `scorr_chart_card.js` (the only file with Pivots / Fib / Channel / GVM / Peers), not
`scorr_analysis_card.js` (that is the separate "A" modal). Blast radius: `scorr_chart_card.js` is
injected on every page by `main.py _MOBILE_HEAD`; real callers are v8/v10 dashboards, /m/gvm,
/m/home, holdings, result corner, CIO, digest. Every change below is inside the sheet's own
elements (`#scorrChartTitle`, `#scorrPeerPane`), so no other component's look changes.

## Item 1 — Rupee net on the summary card
The existing computed numbers were checked first: `open.book.unrealised_rs_one_lot` (whole open
book) and `closed.book.realised_rs_one_lot` (closed-in-the-selected-range only). Neither is the
since-start realised total the Net pts cell describes. So the same per-row formula the endpoint
already applies (`tc_scanner_endpoints._rs`: `(mark − entry) × lot_size × side`, lot from
`futures_universe`, TC_SCANNER_LOT_SIZING_V1) is **summed over the same closed rows `net_pts`
already sums** — the existing number added up, not a new one. Query, in `_record`:

```sql
SELECT h.side, COUNT(*), SUM(CASE WHEN h.exit_reason LIKE 'TARGET%' THEN 1 ELSE 0 END),
       ROUND(SUM(((h.exit_price-h.entry_price)/NULLIF(h.entry_price,0))*100*CASE WHEN h.side='BUY' THEN 1 ELSE -1 END)::numeric,2) AS net_pts,
       MIN(h.entry_ts)::date, MAX(h.exit_ts)::date,
       COUNT(f.lot_size) AS rs_rows,
       ROUND(SUM((h.exit_price-h.entry_price)*f.lot_size*CASE WHEN h.side='BUY' THEN 1 ELSE -1 END)::numeric,2) AS net_rs
FROM tc_scanner_holds h
LEFT JOIN futures_universe f ON f.symbol=h.symbol AND f.lot_size IS NOT NULL
WHERE h.exit_reason<>'OPEN' AND h.exit_price IS NOT NULL AND h.entry_price IS NOT NULL
GROUP BY h.side
```
Run on production (16-Sep): SELL 25 closed, net_pts +41.68, net_rs **+2,10,917.25**; BUY 7 closed,
net_pts −13.78, net_rs **−1,02,222.75** → book **+₹1,08,695** at +27.9 pts over 32 signals (the same
27.9% the card showed). `futures_universe` has one row per symbol (212, 0 duplicates), so the join
cannot multiply rows. New payload keys: `record.BUY/SELL.net_rs_one_lot`, `.rs_rows`.
Card: third cell is now **Net P&L · 1 lot → +₹1,08,695** (sign-coloured), sub line
`+27.9% pts · 32 signals`. A book with no lot-priced row shows a dash, never 0.

## Item 2 — the clipped `(+₹11,006/1..`
Two causes, both fixed in `mobile/tcscan.html` CSS only (markup and numbers untouched):
1. `.scorr-prow .v` was a one-line flex row (cc#2105), so the rupee tail ran past the card edge.
   It is a **column** again: pct on top (same size), rupee/lot below it, right-aligned; `.nm` gets
   `min-width:0` so a long symbol can never push the value column off the card.
2. **The actual slice**: `scorr_theme_r5.css` (injected on every `/m/*` page after the page's own
   styles) has `:root:root:not([data-theme="light"]) .c` — a card rule that also matches this bare
   `.c`: clip-path polygon with a 16px corner cut, border, background. At specificity (0,4,0) it beat
   the cc#2111 reset (0,3,0), so the rupee text rendered as a boxed ribbon with its top-right corner
   cut off. Measured in the harness (computed `clip-path` was the polygon). Reset at (0,5,0):
   `:root:root .scorr-prow .v .c{clip-path:none; …}`. r5's own `.c` panels elsewhere are untouched.

## Item 3 — sheet text follows the theme's own token
`#scorrChartTitle` (and the peers pane's segment label, symbol names, sort-active header, GVM
value) now use `var(--ink, <palette>)` / `var(--muted, <palette>)`: the theme's own text pair
(declared by every theme in `scorr_themes.css` and by `scorr_theme_r5.css`), with the old guessed
palette only as fallback for a host page that defines no token. Verified: on `aquawhite` the title
computes to `rgb(14, 26, 32)` = that theme's `--ink` (#0E1A20), luminance 23 (dark text on white);
on `goldnight` it is `#F5F2EA` = its `--ink`. Dark theme not regressed. Written as a JS string
join, so the source carries no `var(--x, #literal)` fallback (ratchet: 0 new).

## Item 4 — D / W / M table
The Peers pane was **already** a table with Day / Week / Month sortable headers (cc#987, cc#2102) —
but the header row scrolled away with the pane, so after the first few peers the rows read as bare
inline numbers with no column labels (the screenshot). Fix: the table now owns its vertical scroll
(`#scorrPeerScroll` max-height 340px) and the header row is `position:sticky; top:0` with an opaque
panel backdrop (the Symbol header pins on both axes). Sort untouched (`_peerSortBy`, tap Month →
MCX +9.27% first, verified). Buy/Watch/Exit badge (`_bandChip`), GVM value, CARD button and the
trade-card drawer are exactly as before.

## Item 5 — polish
Table in a rounded (10px), 1px-bordered box; row padding 7→8px (6→7 narrow). Spacing and edges
only; no colour-system change.

## Verify
`ast`/`py_compile` clean (`scanners_app_mobile.py`); `node --check` clean (`scorr_chart_card.js`,
both `tcscan.html` scripts); 0 new literal fallbacks in added lines. Playwright at 390px with the
real shared JS, the real `mobile_app.css` (extracted from `mobile_endpoints.MOBILE_CSS` by ast) and
r5 tokens, so the cascade under test is the live one:
- `/m/tcscan` with a real payload (6 newest open + 4 newest closed rows from `tc_scanner_holds` ×
  `futures_universe` × `cmp_prices`, the record from the query above, the real `tc_scanner_spec()`):
  10 rows rendered; Net P&L cell `+₹1,08,695`, sub `+27.9% pts · 32 signals`; no value column past
  its card; rupee/lot stacked under the pct and complete (`(−₹14,040/lot)`); computed clip-path
  `none`; no page overflow.
- Chart card Peers sheet, `aquawhite` and `goldnight`, 7 real peers of CAMS (Exchanges & Ratings -
  Mid; same SQL as `build_peers`): title = `--ink`, symbol text = `--ink`, 5 sortable headers,
  sticky header, Month sort, 7 badges, 7 CARD buttons, rounded box.
- Screenshots looked at (VISUAL_VERIFY_GATE_V1): TC Scanner (card + rows), Peers light, Peers dark.

## Seen, not touched (say so rather than fix silently)
- The three stat values on the summary card (`14.3%`, `60%`, `+₹1,08,695`) render inside a boxed
  tile — that is `mobile_app.css`'s bare `.v` card rule bleeding onto `.cell .v`, the same class of
  bleed cc#2111 documented. Pre-existing on the live page; out of items 1-5.
- `CMP 78.95` / `ENTRY 79.15` tick labels overlap on the rail when the two prices sit close —
  `tcTrkLabels`, do_not_touch (slider bar markup).
- Mobile Screeners untouched. No numbers, thresholds or ratings changed anywhere.
