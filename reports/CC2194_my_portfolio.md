# cc#2194 — My Portfolio tile + /m/portfolio: the client's Adaptive Dashboard row as 3 snapshot cards, then the web /health report as 20 swipeable cards

Founder 17-Sep-2026 15:10 + 15:15 IST (voice). Built 17-Sep-2026 from 16:57 IST server time; the landing time is in the task row.

## What shipped

| File | Change |
|---|---|
| `portfolio_app_mobile.py` | NEW. `GET /m/portfolio` (the page), `GET /api/mobile/portfolio?pid=` and `GET /api/mobile/portfolio/clients`. The snapshot endpoint calls `hr_endpoints._health_portfolios_inner()` — the exact function `/api/health/portfolios` serves the `/adaptive` client shelf from — and passes ONE row through (`pick_row` + `shape`, both pure): value, P&L, basis, start date, Nifty %, alpha, the XIRR trio with its reason, pay-in / payout / net pay-in with the ledger span. Nulls stay nulls. Nothing is re-derived. Default pid 165 (Akshay Gupta, PMS). The clients endpoint is `SELECT id, name FROM hr_portfolios ORDER BY name`. |
| `mobile/portfolio.html` | NEW page, every class `pf-` prefixed. Header: title, the client's name under it, a filter-icon button top-right. **Snapshot** — card 1 VALUE (total value, P&L ₹ and % on the invested amount, alpha vs Nifty 50 with the Nifty figure, XIRR vs Nifty 50 XIRR or the reason it is null; cells Invested · Holdings · Cash), card 2 HOLDINGS (count, top 3 by weight with verdict tag and P&L, sector-mix bar + legend), card 3 CASH FLOWS (pay-in / payout / net pay-in over the ledger span, or "no ledger on record" with dashes, never zeros). Then the action card: rating teaser (overall / 10 · verdict · insight), **View Portfolio Health Report ›**, **Download PDF**. **Report** — a horizontal scroll-snap rail, one card per web section (table below), counter "n / 20", a tappable section index and a dot strip above the rail; each card = eyebrow "n of 20 · web: <section>", title, headline number, one-line verdict, detail (long detail scrolls inside the card, capped at 42 % of the screen). **Picker** — full page: search box on top (live filter), every hr_portfolios name below with its id; tap → `/m/portfolio?pid=`; last pick in `localStorage` (`scorr_pf_pid`); `?pid=` always wins over the memory; unknown pid → error card with a link to the reference portfolio. Views are URL-driven (`?pid=&view=report|pick`), back/forward work. |
| `main.py` | Wiring only: import + `include_router`, `PROTECTED.add("/m/portfolio")`, `NAV_REGISTRY["/m/portfolio"]` (grid-tile tier). Not `_PWA_INJECT_PATHS`: that set is desktop-only theme injection and no `/m/` page is in it (the cc#874 rule, restated at every `/m/` wiring in main.py). |
| `mobile/home.html` | The **My Portfolio** tile, first in the **My Scorr** group, `href /m/portfolio`, donut icon (distinct from SmartGain's briefcase). The old `/m/myportfolio` tile stays hidden (cc#2197). |
| `tests/test_portfolio_app_mobile.py` | 5 tests: default pid 165; `pick_row`; `shape` keeps nulls and writes the no-ledger reason; a ledger row carries the period and the "invested set manually" marker; a missing invested amount says so. 5 passed. |
| `reports/CC2194_my_portfolio.md` | This report. |

Data path: the page fetches the snapshot and the report in parallel — the report from the EXISTING `/api/mobile/health_app/report?pid=` (health_app_mobile.py V2 → `hr_report.build_report`, the web /health report's own dict). Cards 1 and 3 render as soon as the shelf row lands; card 2, the rating teaser and the report button fill when the report lands. The shelf call computes every client (that is what `/adaptive` does too): `/api/mobile/health_app/list` has run at p50 2.1 s on production over the last 14 days, so card 1 is on screen in about that; the report follows.

**PDF (spec item 4).** `hr_report_pdf.py`'s `/api/health/report_pdf_self/{pid}` (cc#655) already returns a signed link to the white-label client PDF for the same portfolio, gated on the same login session the app uses — so the button is a link, not new work, and it is IN. The page opens it exactly the way the web SAVE PDF button does: mint the link, fetch it, save the file under the endpoint's own filename; if the PDF engine answers 503 it falls back to the print view (`html_url`). This is the white-label PDF (hr_report_pdf's own render), not the web print version. If the founder wants a different format for it, that is a new card.

## Web section → mobile card (spec verify item 1)

| # | Web /health section (its label on the page) | Mobile card title | Headline on the card |
|---|---|---|---|
| 1 | Invested · Current Value · P&L (mast) | Snapshot | P&L % · ₹ P&L on ₹ invested; Invested / Current + cash / Realised / Unrealised; holdings cost, cash, basis |
| 2 | Alpha (`hr-alpha-n50`, `hr-alpha-n500`, label) | Alpha vs the index | alpha vs Nifty 50 · since the alpha label; vs Nifty 500; the money return |
| 3 | Scorr Parameters + overall rating / verdict | Rating | overall / 10 · verdict · insight; G V M Q bars |
| 4 | 1-yr Return vs Benchmarks | One-year return | portfolio 1y; Nifty 50 1y; Nifty 500 1y |
| 5 | Valuation & Yield | Valuation & yield | portfolio PE; sector PE; Nifty PE; div / sector / Nifty yield; insight |
| 6 | Portfolio Risk Metrics | Risk | beta; std dev; Sharpe; Sortino; max drawdown; R-squared; risk-free rate; window; excluded names ("unavailable" when null) |
| 7 | Sector Split · Sector Ratings | Sectors | largest sleeve; mix bar; table segment · weight · call (score) |
| 8 | Winners vs Losers | Winners vs losers | winners : losers; bar; ratio; insight |
| 9 | Cap Diversification | Company size | largest band; bar + legend (counts); insight |
| 10 | Quality · GVM Bands | Quality bands | largest band; bar + legend (counts); insight |
| 11 | Top Gainers | Top gainers | best P&L %; up to 5 rows (₹ P&L, weight) |
| 12 | Top Losers | Top losers | worst P&L %; up to 5 rows |
| 13 | Upcoming Results | Upcoming results | count; next name + date; rows (event, days) |
| 14 | Red Flags | Red flags | count; symbol · flag · detail |
| 15 | Replacement Idea | Replacement ideas | count; holding · action · segment · peers; the replacement note |
| 16 | Holdings — Full Detail | Holdings | count; table Stock (segment) · Wt · CMP · P&L · Rating (verdict) · Size, every row, scrolls inside |
| 17 | Latest Result Analysis (+ the cc#662 Results-This-Quarter table) | Results this quarter | reported · yet to report; table Sales · Profit · FY27 est with "numbers awaited"; the analysis snippets with their chip |
| 18 | Key Highlights | What stands out | count; the bullet list |
| 19 | Expert take | Expert take | the paragraph; "research, not advice" |
| 20 | Breakup tab (`rating_breakup`, 21 params × top-25 holdings) | Rating breakup | params × holdings; the matrix, sideways scroll; "+N more holdings (W %)" note as on the web |

Every value on a card is a field of the report dict the web renders (`hr_report.build_report`); nothing is invented. The eyebrow on each card names the web section it re-presents.

## Production facts for the reference pid (17-Sep 16:58 IST server time, run_sql)

- `hr_portfolios` 165 = Akshay Gupta, source PMS, `alpha_start_date` 2025-05-15. 25 `hr_holdings` rows, every one with a buy price. `hr_portfolio_meta`: invested 27,68,000, cash 2,76,000, tracking 2025-05-15 → the shelf's basis is `meta`.
- `hr_ledger` rows for 165: **0**. So on production card 1 shows "XIRR — No ledger on record — XIRR needs dated pay-ins and payouts." and card 3 shows three dashes with the no-ledger sentence — exactly what the /adaptive card does for this client (its ledger blocks are absent, its XIRR line absent). `hr_realised` for 165: 0.
- 14 saved portfolios in the picker, A→Z from Akshay Gupta to Vishal Bhosale.

## Harness (Playwright, real Chromium, `scratchpad/cc2194_test.py`) — ALL PASS at 375×812 (goldnight + aquawhite) + 360×780 overflow check

Fixture = Akshay Gupta's real 25 holdings (qty, avg, `cmp_prices`, `gvm_history`, `input_raw`, `screener_raw`, `momentum_scores` read via run_sql) with the real meta and no ledger, shaped into the shelf row and the report dict by fixture math in the harness (weights, sums, weighted averages; index returns, risk metrics and peers are labelled fixture values). The SAME row is served to `scorr_adaptive.html` and to the mobile page.

- Home: **My Portfolio** is the first tile in My Scorr, `href /m/portfolio`, icon present, 81×87 px; the old `/m/myportfolio` tile is absent.
- Snapshot: title "My Portfolio", "Akshay Gupta" under it, no back arrow; as-of "Akshay Gupta · portfolio #165 · 25 holdings · live prices". Card 1: ₹30.67 L (= total_portfolio), "P&L +₹2.99 L · +10.81% on ₹27.68 L invested", "Alpha vs Nifty 50 +6.7% · Nifty 50 +4.1% over the same window", "XIRR — No ledger on record …", cells Invested ₹27.68 L / Holdings ₹27.91 L / Cash ₹2.76 L. Card 2: SAILIFE 6.6% · CARTRADE 6.3% · KARURVYSYA 5.6% (the top 3 by weight), each linking to `/m/check?sym=`, sector bar with 21 segments, legend of 4 + "+17 more sectors". Card 3: — / — / — and the no-ledger sentence. Action card: "7.3 / 10 · Portfolio health rating · Good", View Portfolio Health Report enabled once the report landed, Download PDF. No sideways overflow.
- **Adaptive card, same fixture row** (`cc2194_adaptive_card.png`): "Akshay Gupta · #165 · from 15 May 2025 · 25 holdings · Invested ₹27,68,000 · Current ₹27,91,207 · P&L +₹2,99,207 (+10.8%) · +6.7% vs N50" — the same Invested, Current, P&L ₹, P&L % and alpha as card 1 (the web prints rupees in full and 1 dp; the app prints lakhs and 2 dp).
- Download PDF → a download named `Portfolio_Health_Akshay_Gupta_17Sep2026.pdf` (the endpoint's filename), message "Saved …".
- Report: title "Health Report", URL `?pid=165&view=report`, 20 cards, 20 dots, "1 / 20"; the 20 eyebrows name the web sections in the table above; cards 343 px wide (viewport − 32), the tallest 500 px (< 72 % of the screen), dots above the rail, the rail scrolls, the page does not; the Holdings card lists all 25 rows; the breakup card 25 rows × 18 params + name; headlines "+10.8%", "7.3 / 10 · Good", "12 : 13". Swipe to card 7 → "7 / 20", dot 7, chip "Sectors" pressed. Tap the "Holdings" chip → "16 / 20". Back arrow → the snapshot, URL `?pid=165`.
- Picker: filter icon → "Choose portfolio", 14 names A→Z, tick on the current one, search focused; "seema" → the two Seema Rajesh Gupta rows; tap AliceBlue → `/m/portfolio?pid=186`, "Seema Rajesh Gupta" under the title (broker suffix trimmed for display, as the web does), `localStorage` 186. Cold open without `?pid=` → 186 (the memory); `?pid=165&view=report` → the rail for 165 (URL wins); `?pid=999` → the error card with the link to the reference portfolio.
- 360 px: no overflow on the snapshot or the rail, cards 328 px. No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2194_*.png`): `375_dark_tile` — the My Scorr group with the donut My Portfolio tile first. `375_dark_snapshot` / `375_light_snapshot` — the three cards and the gold (dark) / aqua (light) action card, values as listed above, the XIRR and cash-flow lines honest about the missing ledger. `375_dark_report1` — "HEALTH REPORT · Akshay Gupta", "1 / 20", chips, dots, the Snapshot card with +10.8% and the four cells. `375_light_report7` — the Sectors card on the light theme with the mix bar and the sector table scrolling inside the card. `375_dark_report16` — the Holdings card with the 25-row table (Rating and Size columns reached by sideways scroll inside the card). `375_light_picker` — the search box with "seema" and the two rows. `adaptive_card` — the web card for the same row.

## Nav (rule 8)

Label **My Portfolio**, URL **/m/portfolio**, Home grid → My Scorr (first tile). Registered in `PROTECTED`, `NAV_REGISTRY` ("grid-tile") and `app_route_map` (`home_grid_group` My Scorr). Not in the NAV array / More sheet, like every other grid-tile page.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/portfolio?pid=165` → `snapshot.total_portfolio`, `pnl`, `pnl_pct`, `alpha_pct`, `nifty_pct` equal the `/api/health/portfolios` row for id 165; `snapshot.xirr` null with `xirr_reason` `no_ledger`; `cash_flows.has_ledger` false.
2. `https://scorr.in/m/portfolio` → the three cards for Akshay Gupta, the rating teaser, View Portfolio Health Report → 20 cards; filter → 14 names; Download PDF saves the white-label PDF (or opens the print view if the engine 503s).
3. `https://scorr.in/m/home` → My Portfolio first in My Scorr, opens /m/portfolio.

## Out of scope

The web /health and /adaptive pages, the report engine and the hr_* tables (untouched, do_not_touch). A different PDF layout (new card if wanted). The old /m/health page stays alive and hidden (cc#2199).
