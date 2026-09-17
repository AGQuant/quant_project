# cc#2165 — Results app PUSH 5/5: closing report for RESULTS_APP_V3, screenshot set, founder review notes

Sprint RESULTS_APP_V3, seven pushes, all on `main`. This card is report and verification only; nothing was changed in code here. Two defects found while writing it are filed as their own cards (cc#2181, cc#2182), per the card's own rule. Written 17-Sep-2026 from 14:07 IST server time (the Started line); the checks below ran between 14:06 and 14:13 IST server time.

## The seven pushes and where they landed

| Push | Card | Commit on `main` | What it did | Report |
|---|---|---|---|---|
| 1/7 | cc#2161 | 8f0363c | Season hero to two median cells; tap a cell for its five leading sectors | `reports/CC2161_results_hero_two_cells.md` |
| 2/7 | cc#2162 | bb6b2d9 | Sector table: Top / Under toggle, All / Large / Mid / Small chips, five rows, View-more sheet; payload rows gain `size` + `pat_n` | `reports/CC2162_results_sector_table.md` |
| 3/7 | cc#2163 | 2d7f58b | Tap a sector for its companies table (QoQ, YoY, GVM) in the shared sheet; `/companies?segment=` | `reports/CC2163_results_companies_sheet.md` |
| 4/7 | cc#2164 | 5b68231 | Companies view honours `?segment=`; movers rails tidy; dead code out; body covers the viewport | `reports/CC2164_results_push4.md` |
| 6/7 | cc#2168 | 6647842 | Upcoming results with Large / Mid / Small / Micro chips, 30-day sheet, no bare BSE codes | `reports/CC2168_results_upcoming.md` |
| 7/7 | cc#2169 | db9b22a | Past results button and full-screen view with size chips and the sortable table; `/companies` rows gain `size` | `reports/CC2169_results_past.md` |
| 5/7 | cc#2165 | this commit | This report | `reports/CC2165_results_app_v3.md` |

The quarter-label bug cc#2167 (`Q1 FY27` vs `Q1FY27`, "no write-up" on every mover) landed on `main` in the same window (commit 659034a) and is why the movers rails and Written up section now show real write-ups.

## Screenshot set — 375 px, goldnight (dark) and aquawhite (light), real Chromium via Playwright

Twenty files in `scratchpad/cc2165_<name>_{dark,light}.png`, every one looked at. Fixture = the production replicate the sprint's harnesses used (87 eligible sectors, the 31 Shipping & Maritime / Retail - Mid company rows plus 13 Private Banks rows, the 11 calendar rows for the next 30 days). Page errors: none on either theme.

| # | File | What it shows (one line) |
|---|---|---|
| 1 | `01_hero_sales` | Hero with the two median cells, sales cell pressed (▴), footnote "Profit median on 704 detailed filings · sales median on all 1,712 · 993 of them are CSV-basic (no profit YoY)", and the strip LEADING SECTORS · SALES GROWTH: Aluminium & Non Ferrous +64.9% (5), Gems & Jewellery - Large +45.7% (8), Electrical Cables +44.2% (6), Shipping & Maritime +40.6% (4, THIN), Renewable Energy - Mid +34.9% (10). |
| 2 | `02_hero_profit` | Same hero with the profit cell pressed and the strip re-ranked by PROFIT GROWTH: Commodity & Chlor-Alkali Chemicals +275.9% (3, THIN), Shipping & Maritime +117.3% (4, THIN), Organic Chemicals - Small +102.0% (4, THIN), Aluminium & Non Ferrous +90.9% (5), IT - Small +89.5% (5); long names ellipsise on one line. |
| 3 | `03_sectors_top_all` | SECTORS THIS SEASON, Top performers / All 87: the same five sectors as row 2 with "N filings · GVM verdict" sub-lines and THIN tags on the first three, Profit YoY green and Sales YoY muted, then "82 more sectors · View all ›", then the movers rails (KMEW with "Read ›", GESHIP with the GVM only; JITFINFRA as the one fall with a dash). |
| 4 | `04_sectors_under_small` | Underperformers / Small 8 pressed: Broking & Wealth Management +4.5% (3, THIN), Cement - Small +20.3% (5), Business Services +30.7% (3, THIN), Retail - Mid +50.0% (5), Building Materials - Glass… +56.0% (4, THIN); "3 more sectors in Small · View all ›". On production Small has eight eligible sectors, so this is the live state, not the thin sentence. |
| 4b | `04b_sectors_under_small_thin_variant` | Fixture variant with only three eligible Small sectors: chip "Small 3", the three rows, and the sentence "Only 3 Small sectors have 3+ filings this season." in the panel; View more hidden. The thin state, proven. |
| 5 | `05_view_more_sheet` | The shared bottom sheet over the dimmed page: "Top performers · All sizes · 87 sectors", the basis line "… tap a sector for its companies", rows from Commodity & Chlor-Alkali +275.9% down through Gems & Jewellery - Large +57.8% (ten visible, the sheet scrolls). |
| 6 | `06_companies_sheet` | Tapping Shipping & Maritime in that sheet: header "Shipping & Maritime / 6 filed of 7 · median profit +117.3% · sales +40.6%", the companies table sorted Profit YoY ▾ (KMEW +472.7%, GESHIP +159.7%, SCI +74.9%, SHREEJISPG +18.9%, then JITFINFRA and SEAMECLTD with the muted CSV-basic dot and a dash), the table scrolling sideways inside the sheet, the cc#1192 line and "All filings, sortable ›". |
| 7 | `07_companies_filtered` | `/m/results?view=companies&segment=Shipping%20%26%20Maritime`: title SHIPPING & MARITIME, "6 companies · Shipping & Maritime · Clear filter", the six-row sortable table. Its footer says "dash = not filed yet" — wrong for CSV-basic rows, filed as cc#2181. |
| 8 | `08_upcoming` | UPCOMING RESULTS list grouped by date (Clara Industries "BSE code 543435 UNRANKED" first, ORISSAMINE 5.36, 18 Sep and 21 Sep groups), "1 more · View all ›", then the wide "Past results · 1,712 companies filed this season" button card, then the How-to-read note. |
| 9 | `09_past_results` | `/m/results?view=past`: PAST RESULTS with the back arrow, "Season Q1 FY27 · 42 filed of 44", chips All 42 / Large 4 / Mid 5 / Small 18 / Micro 15, the table Name · Filed ▾ · Sales YoY · Profit YoY (GVM off to the right), 14 Aug rows first, dashes on CSV-basic rows. |

Both themes render the same layout; the light set differs only in colour (aqua outline on the pressed cell and chips, green/red on white panels). The dotted-underline styling under most text on both themes is the shared `mobile_app.css` bare-selector leak already logged on cc#2166; it is not from this sprint.

## Numbers — as rendered, and the SQL that reproduces them

Re-run on production during this card (the SQL below, `scratchpad/cc2165_replicate.sql`). It reproduces `result_corner_v2()` step by step: universe = `gvm_scores` at its latest `score_date` with `COALESCE(screener_raw.market_cap, gvm_scores.market_cap)`; detailed rows = `fundamentals_history` quarters in the last 520 days, consolidated-preferred, latest vs the row four quarters back, each company's YoY rounded to one decimal **before** the median (as `_pct` does); season = the newest quarter-end anyone has filed (2026-06-30); CSV-basic rows = `screener_raw` at its modal `last_result_quarter` for scored symbols with no scraped history, sales YoY only and no profit YoY (cc#1192); medians per segment over same-season reporters (`percentile_cont(0.5)` = the Python median); `n_used` = rows with a profit reading; average sector mcap over the sector's full universe membership; size cuts at 20,000 and 5,000 Cr (`investment_check.CAP_LARGE_MIN` / `CAP_MID_MIN`); eligible = `n_used >= 3`.

**Headline** — reported 1712 of 1773; median sales YoY +18.4; median profit YoY +22.9 on 704 profit readings; basis 719 detailed / 993 CSV-basic. Same as the hero.

**Size counts** (eligible sectors by average mcap) — Large 47 · Mid 32 · Small 8 · unsized 0 = 87. Same as the chips.

| Five leading sectors by sales growth | Page (screenshot 1) | SQL `top_sales` |
|---|---|---|
| Aluminium & Non Ferrous | +64.9% · 5 | 64.9 (5, Large) |
| Gems & Jewellery - Large | +45.7% · 8 | 45.7 (8, Large) |
| Electrical Cables | +44.2% · 6 | 44.2 (6, Mid) |
| Shipping & Maritime | +40.6% · 4 THIN | 40.6 (4, Mid) |
| Renewable Energy - Mid | +34.9% · 10 | **35.0** (10, Large) |

| Five leading sectors by profit growth | Page (screenshots 2, 3, 5) | SQL `top_profit` |
|---|---|---|
| Commodity & Chlor-Alkali Chemicals | +275.9% · 3 THIN | 275.9 (3, Small) |
| Shipping & Maritime | +117.3% · 4 THIN | 117.3 (4, Mid) |
| Organic Chemicals - Small | +102.0% · 4 THIN | 102.0 (4, Small) |
| Aluminium & Non Ferrous | +90.9% · 5 | 90.9 (5, Large) |
| IT - Small | +89.5% · 5 | 89.5 (5, Small) |

Underperformers, All (screenshot set 04 tapped Under first): Defence - Small −35.5 (7, Mid), Cement - Large & Mid −27.1 (8, Large), Refineries & Exploration - Large −24.6 (9, Large), Holding Companies −20.9 (11, Mid), Broadcasting & OTT −4.3 (7, Mid). Underperformers, Small (screenshot 4): Broking & Wealth Management 4.5 (3), Cement - Small 20.3 (5), Business Services 30.7 (3), Retail - Mid 50.0 (5), Building Materials - Glass, Ceramics & Ply **56.1** (4).

**Three values differ by 0.1 between the screenshots and the SQL** (bold above, plus Organic Chemicals - Small sales 27.4 on the page vs 27.5): the harness fixture came from the earlier replicate, which rounded once after the median; this SQL rounds each company first, exactly as the payload does. The live page reads the payload, so it shows the SQL's values. Nothing in the page code rounds.

```sql
WITH uni AS (
  SELECT DISTINCT ON (g.symbol) g.symbol, COALESCE(g.segment, 'Unclassified') AS segment,
         CASE WHEN regexp_replace(COALESCE(s.market_cap::text, g.market_cap::text, ''), '[,%\s]', '', 'g') ~ '^-?([0-9]+\.?[0-9]*|\.[0-9]+)$'
              THEN regexp_replace(COALESCE(s.market_cap::text, g.market_cap::text), '[,%\s]', '', 'g')::numeric END AS market_cap
  FROM gvm_scores g
  LEFT JOIN screener_raw s ON s.nse_code = g.symbol
  WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
),
fh AS (
  SELECT symbol, period_end, metrics, COALESCE(consolidated, false) AS cons
  FROM fundamentals_history
  WHERE section = 'quarters' AND period_type = 'quarter' AND period_end IS NOT NULL
    AND period_end >= CURRENT_DATE - INTERVAL '520 days'
),
hc AS (SELECT symbol, bool_or(cons) AS has_cons FROM fh GROUP BY symbol),
qr AS (
  SELECT f.symbol, f.period_end,
         COALESCE(
           CASE WHEN regexp_replace(COALESCE(f.metrics->>'Sales', ''), '[,%\s]', '', 'g') ~ '^-?([0-9]+\.?[0-9]*|\.[0-9]+)$' THEN regexp_replace(f.metrics->>'Sales', '[,%\s]', '', 'g')::numeric END,
           CASE WHEN regexp_replace(COALESCE(f.metrics->>'Revenue', ''), '[,%\s]', '', 'g') ~ '^-?([0-9]+\.?[0-9]*|\.[0-9]+)$' THEN regexp_replace(f.metrics->>'Revenue', '[,%\s]', '', 'g')::numeric END
         ) AS sales,
         CASE WHEN regexp_replace(COALESCE(f.metrics->>'Net Profit', ''), '[,%\s]', '', 'g') ~ '^-?([0-9]+\.?[0-9]*|\.[0-9]+)$' THEN regexp_replace(f.metrics->>'Net Profit', '[,%\s]', '', 'g')::numeric END AS pat,
         ROW_NUMBER() OVER (PARTITION BY f.symbol ORDER BY f.period_end DESC) AS rn
  FROM fh f JOIN hc USING (symbol)
  WHERE f.cons = hc.has_cons
),
det AS (
  SELECT a.symbol, a.period_end AS latest_q,
         CASE WHEN a.sales IS NOT NULL AND y.sales IS NOT NULL AND y.sales <> 0 THEN ROUND((a.sales - y.sales) / ABS(y.sales) * 100, 1) END AS sales_yoy,
         CASE WHEN a.pat IS NOT NULL AND y.pat IS NOT NULL AND y.pat <> 0 THEN ROUND((a.pat - y.pat) / ABS(y.pat) * 100, 1) END AS pat_yoy,
         'detailed' AS basis
  FROM qr a LEFT JOIN qr y ON y.symbol = a.symbol AND y.rn = 5
  WHERE a.rn = 1
),
se AS (SELECT MAX(latest_q) AS season_end FROM det),
sl AS (SELECT last_result_quarter FROM screener_raw WHERE last_result_quarter IS NOT NULL GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1),
csv AS (
  SELECT DISTINCT ON (UPPER(c.nse_code)) UPPER(c.nse_code) AS symbol,
         CASE WHEN c.sn IS NOT NULL AND c.sy IS NOT NULL AND c.sy <> 0 THEN ROUND((c.sn - c.sy) / ABS(c.sy) * 100, 1) END AS sales_yoy,
         NULL::numeric AS pat_yoy, 'basic' AS basis
  FROM (
    SELECT s.nse_code,
           CASE WHEN regexp_replace(COALESCE(s.sales_latest_quarter::text, ''), '[,%\s]', '', 'g') ~ '^-?([0-9]+\.?[0-9]*|\.[0-9]+)$' THEN regexp_replace(s.sales_latest_quarter::text, '[,%\s]', '', 'g')::numeric END AS sn,
           CASE WHEN regexp_replace(COALESCE(s.sales_preceding_year_quarter::text, ''), '[,%\s]', '', 'g') ~ '^-?([0-9]+\.?[0-9]*|\.[0-9]+)$' THEN regexp_replace(s.sales_preceding_year_quarter::text, '[,%\s]', '', 'g')::numeric END AS sy
    FROM screener_raw s, sl
    WHERE s.last_result_quarter = sl.last_result_quarter AND s.nse_code IS NOT NULL AND s.nse_code <> ''
  ) c
  WHERE NOT EXISTS (SELECT 1 FROM det d WHERE d.symbol = UPPER(c.nse_code))
    AND EXISTS (SELECT 1 FROM uni u WHERE u.symbol = UPPER(c.nse_code))
),
same AS (
  SELECT d.symbol, d.sales_yoy, d.pat_yoy, d.basis FROM det d, se
  WHERE d.latest_q = se.season_end AND EXISTS (SELECT 1 FROM uni u WHERE u.symbol = d.symbol)
  UNION ALL
  SELECT symbol, sales_yoy, pat_yoy, basis FROM csv
),
sm AS (SELECT s.symbol, s.sales_yoy, s.pat_yoy, s.basis, u.segment FROM same s JOIN uni u ON u.symbol = s.symbol),
summ AS (
  SELECT COUNT(*) AS reported, (SELECT COUNT(*) FROM uni) AS total,
         ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY sales_yoy)::numeric, 1) AS med_sales,
         ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pat_yoy)::numeric, 1) AS med_pat,
         COUNT(pat_yoy) AS pat_n,
         COUNT(*) FILTER (WHERE basis = 'detailed') AS detailed,
         COUNT(*) FILTER (WHERE basis = 'basic') AS basic
  FROM sm
),
sec AS (
  SELECT segment, COUNT(*) AS reported,
         ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY sales_yoy)::numeric, 1) AS sales_yoy,
         ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pat_yoy)::numeric, 1) AS pat_yoy,
         COUNT(pat_yoy) AS n_used
  FROM sm GROUP BY segment
),
mc AS (SELECT segment, ROUND(AVG(market_cap)) AS avg_mcap, COUNT(*) AS total FROM uni GROUP BY segment),
sz AS (
  SELECT s.segment, s.reported, m.total, s.sales_yoy, s.pat_yoy, s.n_used, m.avg_mcap,
         CASE WHEN m.avg_mcap IS NULL THEN NULL WHEN m.avg_mcap >= 20000 THEN 'Large' WHEN m.avg_mcap >= 5000 THEN 'Mid' ELSE 'Small' END AS size
  FROM sec s LEFT JOIN mc m ON m.segment = s.segment
),
el AS (SELECT * FROM sz WHERE n_used >= 3)
SELECT 'summary' AS set, 1 AS rk, 'reported / total / pat_n' AS label, reported::numeric AS v1, total::numeric AS v2, pat_n::int AS n, NULL::text AS size FROM summ
UNION ALL SELECT 'summary', 2, 'median sales yoy / median pat yoy', med_sales, med_pat, pat_n::int, NULL FROM summ
UNION ALL SELECT 'summary', 3, 'detailed / basic', detailed, basic, NULL, NULL FROM summ
UNION ALL SELECT 'sizes', 1, COALESCE(size, 'unsized'), COUNT(*), NULL, NULL, size FROM el GROUP BY size
UNION ALL (SELECT 'top_sales', ROW_NUMBER() OVER (ORDER BY sales_yoy DESC NULLS LAST), segment, sales_yoy, pat_yoy, n_used::int, size FROM el ORDER BY sales_yoy DESC NULLS LAST LIMIT 5)
UNION ALL (SELECT 'top_profit', ROW_NUMBER() OVER (ORDER BY pat_yoy DESC NULLS LAST), segment, pat_yoy, sales_yoy, n_used::int, size FROM el ORDER BY pat_yoy DESC NULLS LAST LIMIT 5)
UNION ALL (SELECT 'under_all', ROW_NUMBER() OVER (ORDER BY pat_yoy ASC NULLS LAST), segment, pat_yoy, sales_yoy, n_used::int, size FROM el ORDER BY pat_yoy ASC NULLS LAST LIMIT 5)
UNION ALL (SELECT 'under_small', ROW_NUMBER() OVER (ORDER BY pat_yoy ASC NULLS LAST), segment, pat_yoy, sales_yoy, n_used::int, size FROM el WHERE size = 'Small' ORDER BY pat_yoy ASC NULLS LAST LIMIT 5)
ORDER BY 1, 2, 3;
```

## What the founder should look at — three decisions Fable made without him

**(a) Ranking basis.** Sectors are ranked by the **median profit growth vs last year**, with a **floor of three profit readings** (`n_used >= 3`, the payload's own tiny-base rule) and a **THIN tag at three or four**. Today that puts three thin sectors at the top of Top performers (Commodity & Chlor-Alkali on 3 filings, Shipping & Maritime and Organic Chemicals - Small on 4). One line changes it: a floor of five would drop those three and start the list at Aluminium & Non Ferrous; ranking by sales instead of profit would put Aluminium & Non Ferrous, Gems & Jewellery - Large and Electrical Cables first.

**(b) Sector size.** A sector is Large / Mid / Small by its **average market cap over its full universe membership**, cut at 20,000 and 5,000 Cr — the same cuts `/m/sector` and `investment_check` use — never by the reporters alone, so the size does not move as filings trickle in. One consequence to rule on: the sector names carry the data source's own tier word, so **"Retail - Mid" sits under the Small chip** (average 4,070 Cr) and **"Defence - Small" under Mid**. The chips are right by the rule; the names say something else. Options are to leave it, to label the chips "by avg. sector mcap", or to size by the name's word instead. Fable's choice was the rule, not the word.

**(c) CSV-basic profit.** A company whose only source is the daily CSV shows **a dash for profit YoY**, not the CSV's profit line (cc#1192: the CSV profit is not the post-minority Net Profit the filed history is struck on, and disagreed on 4 of 9 checked names). Sales YoY still shows. This is why 993 of 1,712 filers carry no profit YoY and the profit median rests on 704. Overruling it means showing a number that is wrong by a fifth on some names; the alternative is to widen the scrape universe so more names become detailed.

## Checks the card asked for

- `theme_validate` on `mobile/results.html` (run against the deployed file): **clean** — 0 raw primitives, 0 literal fallbacks, level with the baseline. The same run reports the fallback ratchet red on two files outside this sprint (`mobile/v8.html` +14, `scorr_bell.js` +5) — filed as cc#2182, not touched here.
- `ast.parse` on `results_app_mobile.py`: OK. The full test suite after push 7: 55 passed, 10 skipped.
- **No NAV change was needed.** `/m/results` already has its entry in the `NAV` array in `pwa_endpoints.py` (line 485), is in `PROTECTED` and in `NAV_REGISTRY` in `main.py`; the sprint added views under the same route (`?view=companies`, `?view=past`, `?segment=`, `?perf=`, `?size=`, `?up=`), no new page.

## Fable's diff + DB pass, from this report alone

1. Diff: the six commits in the first table, plus 659034a (cc#2167).
2. DB: run the SQL above; expect the headline, the size counts and the two top-five lists as stated.
3. Live: `https://scorr.in/api/mobile/results_app/season` → `summary` = the headline; `sectors[]` with `pat_n >= 3` sorted by `pat_yoy` desc → the profit list, by `sales_yoy` desc → the sales list; count `size` → 47 / 32 / 8. `https://scorr.in/m/results` on a phone: screenshots 1–9 in that order.

## Defects found, filed as cards (nothing fixed here)

- **cc#2181** (P2 UI): companies view footer wording "dash = not filed yet" → should say CSV-basic, like the Past results footer and the companies sheet.
- **cc#2182** (P2 THEME): fallback ratchet regressions on `mobile/v8.html` and `scorr_bell.js`, outside this sprint.

## Out of scope

Web `/result-corner` parity and alerts on sector moves — after the founder review, as the card says.
