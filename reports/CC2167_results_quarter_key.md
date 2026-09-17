# cc#2167 — /m/results: "no write-up" on every mover and "Nothing written yet" under 774 analyses

Founder screenshot 17-Sep-2026 11:41 IST. Fixed 17-Sep-2026, landed on main 13:20 IST (times corrected to the git commit clock).

## The bug, in one line

`results_app_mobile.py` looked up `result_analysis_v2` with the season's **display** label `Q1 FY27` (from `result_corner._fq_label`, with a space) while the table stores `Q1FY27` (no space), so `_written_set()` came back empty (every company row `written: false`) and `result_analysis_v2_list(quarter='Q1 FY27')` returned nothing.

Proved on production rows (≈13:17 IST):

| Lookup | Rows |
|---|---|
| `WHERE quarter = 'Q1 FY27'` (what the page sent) | 0 |
| `WHERE quarter = 'Q1FY27'` (what is stored) | 773 |
| …of which `symbol = 'CHENNPETRO'` | 1 |

`SELECT quarter, COUNT(*) FROM result_analysis_v2 GROUP BY 1` → `Q1FY27` 773 (polished 02–29 Aug), `Q4FY26` 1.

## The fix (one normalisation, `results_app_mobile.py`)

- New `_qkey(quarter)` = `str(quarter or "").replace(" ", "")`, documented as the ONE place the display label becomes the stored key.
- `_written_set(cur, quarter)` queries with `_qkey(quarter)` (this covers both `/api/mobile/results_app/season` and `/api/mobile/results_app/companies`, which share it).
- The `result_analysis_v2_list(limit=12, quarter=…)` call in the season endpoint passes `_qkey(quarter)`.
- `result_corner._fq_label()` is untouched (the label with the space is what the page displays); no stored row was rewritten. The season payload still reports `season.quarter = "Q1 FY27"` for display.

## Other readers of the stored quarter (spec item 2) — none defective, no separate card needed

| Reader | What it does | Status |
|---|---|---|
| `pwa_endpoints.py` 2262–2268 (web company card JS) | compares on `_qkey()` = whitespace-stripped upper form; comment already names the `Q1 FY27` vs `Q1FY27` difference | correct |
| `digest_v3.py` 818 `_fq_label` | its own label function emits `Q1FY27` "in result_analysis_v2's own spelling (no space)" | correct |
| `results_endpoints.py` 1294 (`normalize_doc_quarter` family) | normalises doc quarter labels with `replace(" ", "")` | correct |
| `results_endpoints.py` 1383 `result_analysis_v2_queue` | hard-codes the stored spelling `'Q1FY27'` | correct spelling (hard-coded season, a separate concern, not a mismatch) |
| `results_endpoints.result_analysis_v2(symbol, quarter)` / `result_analysis_v2_list(limit, quarter)` | compare `quarter=%s` with whatever the caller passes | no caller outside `results_app_mobile.py` passes a quarter (repo grep) |
| `max_native_cards.py` 722 | prints the season label, no comparison | n/a |

## Proof

- **Production replay** of the two lookups with the old key and the new key: the table above.
- **The list query** `result_analysis_v2_list`'s SQL with `quarter = 'Q1FY27' LIMIT 12` on production → 12 rows with teasers (AXISCADES, CAMPUS, JKLAKSHMI, NEPHROPLUS, RCF, SFL, SPARC, TANLA, GOKULAGRO, JKPAPER, EBGNG, GMRP&UI; polished 29 Aug).
- **Unit tests** `tests/test_results_quarter_key.py`: `_qkey` strips the space; `_written_set` binds `('Q1FY27',)` for the label `Q1 FY27` (fake cursor); a real-DB test (skips without `DATABASE_URL`) that the written set for `Q1 FY27` contains CHENNPETRO and has more than 700 names. 2 passed, 1 skipped in the sandbox. (The results backend imports `psycopg2` at module level; the test installs an empty stub only when that driver is absent, so the no-DB checks run on every seat.)
- **Page harness** (`scratchpad/cc2167_test.py`, Playwright 375 px dark + light) with the season payload stubbed two ways — as production served it before the fix (empty `written.rows`, every `written: false`) and as it is served now (the 12 real written rows from the query above; the movers' numbers are SAMPLE because the sandbox cannot run `result_corner_v2`, only the `written` flag matters here): before → 0 Written up rows, "Nothing written yet", CHENNPETRO "no write-up"; after → 8 Written up rows, all with teasers, CHENNPETRO / APOLLOTYRE / URBANCO / AEQUS "Read ›", the one sample mover without an analysis still "no write-up", no sideways scroll, no page errors. ALL PASS.
- **Screenshots looked at** (`cc2167_before_dark.png`, `cc2167_after_goldnight.png`, `cc2167_after_aquawhite.png`): before shows the founder's picture (movers with "no write-up", an empty "Nothing written yet" card under "774 analyses · last 29 Aug"); after shows eight written rows with dates and teasers and "Read ›" on the mover cards. Observation, outside this card: the results page's hero labels carry the dotted underline / boxed-value styling from the shared `mobile_app.css` bare selectors (the cc#2155 root cause); the Results sprint hero rework (cc#2161) is where that gets addressed.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/results_app/season` → `written.rows` has 12 entries (the page shows 8), `written.total` 774, `movers.up[0].written` true for CHENNPETRO (if it is still the top jump), `season.quarter` still `Q1 FY27`.
2. `https://scorr.in/api/mobile/results_app/companies` → the rows for CHENNPETRO, APOLLOTYRE, AEQUS, URBANCO carry `written: true`.
3. `https://scorr.in/m/results` on a phone: Written up lists names with teasers; the CHENNPETRO card reads "Read ›".
