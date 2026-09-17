# cc#2174 (P0) -- custom screener /run returned bare M-scores instead of row objects

Founder screenshot 17-Sep 12:21 IST: "Show names" on Size Large + Mid -> the sheet said 247 names and every
row was dashes. Fable reproduced it on production at 12:24 (cc#2158 reopened).

## 1. Root cause

`custom_screener_app.run_sql` built the row payload as
`(SELECT COALESCE(json_agg(m), '[]'::json) FROM (SELECT ..., ROUND(m_score::numeric, 2) AS m, ... FROM matched ... LIMIT n) m)`.
The subquery had both a COLUMN named `m` and a TABLE ALIAS named `m`. Postgres resolves the bare `m` inside
`json_agg(m)` as the column, so `rows` was `[8.59, 8.12, ...]` -- the M scores -- not objects. The counts were
right, so every count-based check passed. My own production run at 12:05 printed `rows->0 = 8.59` and I read
it as a tool quirk instead of the bug showing itself. `g` and `v` would have collided the same way.

## 2. Fix (one alias, one SQL string)

`json_agg(rowx) FROM (...) rowx`. `rowx` is not a column of the row subquery. Nothing else changed: the
registry, the counts, the page, the endpoint contracts, the `m` column the page reads.

## 3. Evidence on production (the module's exact statement, run 12:35-12:37 IST)

| Call | Result |
|---|---|
| `size=large,mid&gvm=good&momentum=strong&invest=high` | total 32; `rows` is an array of 32 **objects**; `rows->0` = `{"symbol":"GLENMARK","company_name":"Glenmark Pharmaceuticals Ltd","segment":"Pharma - Large Formulations","cap":"mid","gvm":8.52,"verdict":"Excellent","g":8.21,"v":8.75,"m":8.59,"invest_score":8.96,"invest_band":"STRONG_BUY","price":2413.7,"year_return":16.03,"w52":82.9}`; keys = the full 14; invest_score never rises down the list |
| `size=small&valuation=reasonable&invest=high&trend=up` | total 88; first three ROW OBJECTS: KARURVYSYA 9.76 STRONG_BUY, CEIGALL 9.76 STRONG_BUY, CUB 9.76 STRONG_BUY |
| `size=large,mid` (the founder's case) | total 247; first eight objects GLENMARK 8.96, NAVINFLUOR 8.80, IPCALAB 8.72, POLICYBZR 8.72, WELCORP 8.52, LLOYDSME 8.40, MCX 8.30, APLAPOLLO 8.16 -- saved verbatim as `scratchpad/cc2174_real_run.json` |

## 4. Tests

- **Real-DB test, in the repo**: `tests/test_custom_screener_rows.py` runs `run_sql({'size':['large']}, limit=2)`
  against `DATABASE_URL` and asserts `rows[0]` is a dict carrying `symbol, gvm, g, v, m, invest_score` (and the
  full 14-key set), plus the sort and the empty-selection shape. It skips loudly where `DATABASE_URL` is unset
  -- this sandbox has none, so here it reports `3 skipped`; the same three statements were executed on
  production through `run_sql` (table above). Fable's seat and Railway have the URL and run it for real.
- **Unit test** (`scratchpad/cc2158_unit.py`): now asserts the `json_agg` alias is not one of the row
  subquery's column names.
- **Sheet check** (`scratchpad/cc2174_test.py`, dark + light): the page fed the REAL production output for
  `size=large,mid` (count 247, the eight row objects above) renders `GLENMARK / NAVINFLUOR / IPCALAB`, GVMs
  `8.52 / 8.15 / 7.75`, `Invest 8.96 · Strong buy`, zero dashes. Negative control: the pre-fix payload
  (`[8.59, 8.12, ...]`) renders eight dash rows -- this check would have caught the bug. Screenshot
  `cc2174_sheet_real_dark.png` looked at: names and numbers on every row.

## 5. Bookkeeping

cc#2158 was reopened by Fable; its status goes back to done only through this card, after Fable re-runs the
fixed statement on production and sees objects. Lesson written into both threads: an endpoint that returns
rows as a JSON aggregate is verified by fetching one real row object, never by a count.

## 6. Live check after deploy

- https://scorr.in/api/mobile/custom_screener/run?size=large,mid -> `rows[0]` is an object with `symbol`
- https://scorr.in/m/screeners?view=custom&size=large,mid -> Show names -> names on every row
