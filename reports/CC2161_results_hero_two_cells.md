# cc#2161 — Results app PUSH 1/5: season hero to two cells, tap a median to see its five leading sectors

Sprint RESULTS_APP_V3 (Fable-owned; founder handover 17-Sep-2026 11:37 IST). Built 17-Sep-2026, 13:58–14:12 IST. Page only: `mobile/results.html`. No backend change (`result_corner.py`, `results_app_mobile.py` untouched).

## What changed in the hero

- **Two cells, not three.** The `.kg` grid is `1fr 1fr`: Median sales YoY and Median profit YoY. Each cell is a `<button>` (min-height 44 px, `aria-pressed`), with a small ▾/▴ hint on its label; the pressed cell carries the brand outline. The cells use prefixed classes (`cell-k`, `cell-v`) so the shared `mobile_app.css` bare `.k`/`.v` rules cannot restyle them (the old cells showed the boxed "value pill" look for that reason).
- **The "On N filings / full / basic" cell is gone.** Its facts are ONE footnote line under the cells, in plain words, from the existing summary keys: "Profit median on **{pat_n}** detailed filings · sales median on all **{reported}** · **{basis.basic}** of them are CSV-basic (no profit YoY)". A missing number prints as a dash, never 0.
- **Leading sectors strip.** Tapping a cell opens a strip directly under the footnote: "Leading sectors · sales|profit growth" with FIVE rows — sector name, its median for that metric (signed %, win/loss colour), "{n_used} filings", and a small THIN tag when n_used is 3 or 4. Source is `d.sectors` from the existing season payload; filter `n_used >= 3` (the payload's own tiny-base rule); ranked by the tapped metric, descending. The other cell's strip is hidden; tapping the same cell again collapses; sales is open on load (the founder's own example was the 18% sales figure). If no sector has three profit readings the strip says so in words.
- **Each sector row is a link** to `/m/results?view=companies&segment=<sector>`. Until push 3 lands that is the companies view (the `segment` parameter is ignored today and is what push 4 (cc#2164) makes the companies view honour, so the link needs no change later).
- Filed count, PAT split bar and legend are unchanged. Contract tokens only, 0 literal fallbacks.

## Harness (Playwright, real Chromium, `scratchpad/cc2161_test.py`) — ALL PASS at 375×812 and 390×844, goldnight + aquawhite

| Measure | 375 | 390 |
|---|---|---|
| cells | 2, same top (263 px), 153 px wide each, 83 px tall | 2, 160 px wide each, 83 px tall |
| footnote | 2 lines | 2 lines |
| strip on load | sales, 5 rows | sales, 5 rows |
| sideways scroll | none (scrollWidth 375) | none (390) |

Checked on every run: cell labels and values (+18.4% / +22.9%) from the payload; footnote text "Profit median on 704 detailed filings · sales median on all 1,712 · 993 of them are CSV-basic (no profit YoY)"; sales cell pressed and the sales strip open on load; the five rows equal the fixture's top five by sales with `n_used >= 3`; rows ≥ 40 px, each linking to the companies view with its segment; the two synthetic tiny-base rows (n_used 1 and 2) never appear; tap profit → the top five by profit, sorted descending, all `n_used >= 3`, the thin tag on exactly the rows with 3–4 filings; tap profit again → strip collapses, no cell pressed; tap sales → re-sorted by sales; no page errors.

**Screenshots looked at** (`scratchpad/cc2161_*.png`): `before_375_dark` — the founder's picture: three cells in a row, the third ("ON 704 FILINGS / 719 full · 993 basic") wrapping to four lines. `375_dark_sales` — two equal cells, the sales cell outlined in gold with ▴, the footnote in two lines, then "LEADING SECTORS · SALES GROWTH / MEDIAN · FILINGS" with Aluminium & Non Ferrous +64.9% (5), Gems & Jewellery - Large +45.7% (8), Electrical Cables +44.2% (6), Shipping & Maritime +40.6% (4, THIN), Renewable Energy - Mid +34.9% (10). `375_light_profit` and `390_dark_profit` — the profit cell outlined, the strip re-ranked: Commodity & Chlor-Alkali Chemicals +275.9% (3, THIN), Shipping & Maritime +117.3% (4, THIN), Organic Chemicals - Small +102.0% (4, THIN), Aluminium & Non Ferrous +90.9% (5), IT - Small +89.5% (5); long names ellipsise on one line. Observation outside this card: the page still shows the dotted-underline styling on most text from the shared `mobile_app.css` bare selectors (also visible in the before picture); logged as a finding on cc#2166.

## Strip values vs the season payload (spec verify item 3)

The sandbox cannot call `/api/mobile/results_app/season`, so the payload's sector rows were replicated on production with the same method as `result_corner_v2()` (same-quarter reporters = the newest fundamentals quarter-end, 2026-06-30; detailed rows from `fundamentals_history` consolidated-preferred with YoY against the row four quarters back; CSV-basic rows from `screener_raw` at the modal `last_result_quarter` Q1FY27 with no profit YoY; per-segment medians; `n_used` = profit readings). The replicate reproduces the spec's own evidence exactly: reported 1712 of 1773, median sales YoY +18.4%, median profit YoY +22.9% on 704 detailed filings, basis split 719 detailed / 993 basic, 87 sectors with three or more profit readings. (One rounding difference is possible in principle: the payload rounds each company's YoY to one decimal before the median, the replicate rounds the median; the headline sectors agree to one decimal.)

| Rank | Strip: sales growth (page) | Replicate: top 5 by sales_yoy, n_used ≥ 3 |
|---|---|---|
| 1 | Aluminium & Non Ferrous +64.9% · 5 filings | Aluminium & Non Ferrous 64.9 (5) |
| 2 | Gems & Jewellery - Large +45.7% · 8 | Gems & Jewellery - Large 45.7 (8) |
| 3 | Electrical Cables +44.2% · 6 | Electrical Cables 44.2 (6) |
| 4 | Shipping & Maritime +40.6% · 4 THIN | Shipping & Maritime 40.6 (4) |
| 5 | Renewable Energy - Mid +34.9% · 10 | Renewable Energy - Mid 34.9 (10) |

| Rank | Strip: profit growth (page) | Replicate: top 5 by pat_yoy, n_used ≥ 3 |
|---|---|---|
| 1 | Commodity & Chlor-Alkali Chemicals +275.9% · 3 THIN | Commodity & Chlor-Alkali Chemicals 275.9 (3) |
| 2 | Shipping & Maritime +117.3% · 4 THIN | Shipping & Maritime 117.3 (4) |
| 3 | Organic Chemicals - Small +102.0% · 4 THIN | Organic Chemicals - Small 102.0 (4) |
| 4 | Aluminium & Non Ferrous +90.9% · 5 | Aluminium & Non Ferrous 90.9 (5) |
| 5 | IT - Small +89.5% · 5 | IT - Small 89.5 (5) |

Fable's evidence on the card listed Shipping & Maritime, Aluminium & Non Ferrous and IT - Small with the same numbers; the replicate also carries Commodity & Chlor-Alkali Chemicals (3 filings) and Organic Chemicals - Small (4) above two of them, both tagged THIN on the page — the tag exists for exactly this.

## Live check (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/results_app/season` → sort `sectors` with `n_used >= 3` by `pat_yoy` desc and by `sales_yoy` desc; the first five of each must equal the strip on `https://scorr.in/m/results` after tapping the profit / sales cell.
2. On the phone: two cells side by side, no wrapping; footnote one or two lines; tap profit → five rows, thin tags on 3–4 filings; tap again → closes; tap a sector row → the companies view.

## Out of scope (by spec)

Sector table (push 2, cc#2162), companies sheet (push 3, cc#2163) — the strip rows will point at that sheet once it exists.
