# cc#2180 (P0) -- /m/mf dead: `ORDER BY "ret_1y" is ambiguous` in the V15 screener

Founder screenshot 17-Sep 12:29 IST: `/m/mf` shows `Could not load: AmbiguousColumn: ORDER BY "ret_1y" is
ambiguous ...` and, below it, a dark band under the light-theme page.

## 1. Root cause and when it broke

`mf_pipeline.v15_screener` selects `m.ret_1y` (mf_master) AND `d.ret_1y` (mf_derived_metrics), so the
statement has two output columns named `ret_1y`; its `ORDER BY` named `ret_1y` without a table alias.
Postgres rejects the whole statement, on every sort path (`mqs` and `aum` both tiebreak on `ret_1y`).

`git log -S'ret_1y' -- mf_pipeline.py` (the clone is complete, `--unshallow` was not needed): the join and the
second `ret_1y` arrived in **`ded7769` = cc#777, 01-Aug-2026** ("screener renders derived metrics as a
labelled gap-filler"). Every call of `v15_screener` has failed since: the web `/v15` page's own endpoint
**is** this function (`@router.get("/api/v15/screener")`), so the web screener was down too; the mobile
`/m/mf` list (Fable's `mf_app_mobile.mobile_mf_list`, 10-Sep) inherited it on day one. `ops_log` carries no
record of the failure (the selftest endpoint was never hit), which is why it sat unnoticed for six weeks.

Reproduced on production 12:40 IST with the exact statement: `ORDER BY "ret_1y" is ambiguous`.

## 2. Fix (three ORDER BY strings, nothing else in the query)

| sort | before | after |
|---|---|---|
| `1y` | `ret_1y DESC NULLS LAST, aum_cr DESC NULLS LAST, name` | `m.ret_1y DESC NULLS LAST, m.aum_cr DESC NULLS LAST, m.name` |
| `aum` | `aum_cr DESC NULLS LAST, ret_1y DESC NULLS LAST, name` | `m.aum_cr DESC NULLS LAST, m.ret_1y DESC NULLS LAST, m.name` |
| `mqs` (default) | `mqs DESC NULLS LAST, ret_1y DESC NULLS LAST, name` | `s.mqs DESC NULLS LAST, m.ret_1y DESC NULLS LAST, m.name` |

`m.ret_1y` is the official return the row displays (`"ret_1y": r1y` comes from `m`), so the sort matches
what is shown; the derived `d.ret_1y` only fills gaps and stays a labelled fallback. Every other `ret_1y`
use in `mf_pipeline.py` is either already qualified (`m2.ret_1y`, `m.ret_1y` in the fund/peer queries) or a
single-table statement (category averages, backfill) -- none joins `mf_derived_metrics`, so nothing else
needed the alias.

## 3. Production runs of the fixed statements (12:41 IST)

| sort | first rows |
|---|---|
| `mqs` | HDFC Defence Fund 73.12 (1y 19.49), Quant Value Fund 72.51, HDFC Pharma and Healthcare 71.07, Kotak MNC 70.77, ICICI Pru US Bluechip 70.2 |
| `1y` | TRUSTMF Small Cap 24.72%, Motilal Oswal Special Opportunities 23.52%, ICICI Pru US Bluechip 22.95% |
| `aum` | Parag Parikh Flexi Cap 1,61,795 Cr, HDFC Flexi Cap 1,06,496 Cr, HDFC Mid Cap 1,00,858 Cr |
| category `Mid Cap Fund` (mqs) | 36 funds: HSBC Midcap 65.96, Kotak Midcap 62.03, WhiteOak Mid Cap 61.13 |

Universe behind the screener: 510 direct-growth equity funds.

## 4. Real-DB test, in the repo -- `tests/test_mf_screener_sort.py`

`v15_screener(category='', sort=…, limit=5)` for `mqs`, `1y` and `aum` must return rows with the displayed
keys; one category (`Mid Cap Fund`) must return rows; the `1y` sort must follow the displayed `ret_1y`
descending. Skips loudly without `DATABASE_URL` (this sandbox has none -> `5 skipped` here; the same
statements ran on production, section 3). Fable's seat and Railway run it for real.

## 5. Item 5 -- the dark band under a short page (`mobile/mf.html`)

Root cause, measured: `mobile_app.css` paints `html,body` with `var(--field, <dark hex>)`, but the theme
tokens live on `body[data-theme]`, so `<html>` cannot see `--field` and takes the dark fallback while
`<body>` takes the theme's field. On the error state the body was **229px of an 812px viewport**, body
background `rgb(244,249,251)` vs html `rgb(10,15,30)` -- the navy band the founder saw (dark theme has the
same gap: field `rgb(10,10,12)` vs html `rgb(10,15,30)`). Fix on this page: `body{min-height:100vh;
min-height:100dvh; background:var(--field)}` -- the body now covers the viewport (812 of 812) in both themes.
The same shared rule reaches every mobile page; a page-wide fix belongs with cc#2166's sweep (noted in the room).

## 6. Checks -- `scratchpad/cc2180_test.py`, `=== ALL cc#2180 PAGE CHECKS PASS ===` (375px, light + dark)

Before: the shipped page in the founder's error state, body 229px, html colour differing -> the band.
After: body 812px in the error state; the list renders the six real rows of the fixed `mqs` sort (HDFC
Defence 73 first), as-of `425 scored of 510 equity funds` (425 = SAMPLE for `v15_stats`, 510 real), body
taller than the viewport; tapping the `1y` sort asks the endpoint for `sort=1y`; no page errors. Screenshots
looked at: `cc2180_before_error_light.png` (error text, light top, dark navy lower half),
`cc2180_after_error_light.png` (light to the bottom), `cc2180_list_aquawhite.png` and `cc2180_list_goldnight.png`
(the fund rows with score, 1y, 3y, AUM, ER).

## 7. Live check after deploy

- https://scorr.in/api/mobile/mf_app/list?sort=mqs -> `count` > 0 (also `sort=1y`, `sort=aum`, `category=Mid%20Cap%20Fund`)
- https://scorr.in/api/v15/screener -> rows (the web /v15 screener is back too)
- https://scorr.in/m/mf -- the list, and no dark band under a short page
