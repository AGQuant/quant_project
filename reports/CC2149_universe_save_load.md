# cc#2149 — QB Universe builder: SAVE / LOAD universe definitions

Date: 17-Sep-2026. Files: `qb_universe_builder.py`, `scorr_qb_universe.html`, this report.
Page: `/qb/universe2` (also step 1 of `/quant-basket`'s Build-a-Basket flow).

## What is live now

**Table `qb_universe_defs`** — CREATE only, no ALTER anywhere (MAINTENANCE_LOCK_RULE). Created in
production 17-Sep before the code landed; the save path also runs `CREATE TABLE IF NOT EXISTS`.

| Column | Meaning |
|---|---|
| `basket_name`, `def_name` (DEFAULT 'default') | the key, `UNIQUE (basket_name, def_name)` — the same convention as `qb_entry/exit/risk_rulesets` (basket_name + a name defaulting to 'default'), so a reconciliation can join all four on basket_name |
| `pools` jsonb | the pool set, in the order picked (cc#2143 multi-select) |
| `filters` jsonb | every APPLIED row in the page's own order: `key, op, min, max, list, duration, n, k, note` |
| `as_of_score_date`, `preview_count` | the score_date the definition was last previewed on, and the count that preview gave |
| `created_at`, `updated_at` | |

**Endpoints** (same router as the rest of `/api/qb/universe2/*`; the preview SQL is untouched):

- `POST /api/qb/universe2/save` — body = the page state; upsert on `(basket_name, def_name)`. Rejects an unknown pool or filter key, a non-number, a note over 500 characters, a blank name (HTTP 400 with the reason). Returns `created` (insert vs overwrite) and `preview_query`.
- `GET /api/qb/universe2/load?basket_name=&def_name=default` — the saved row plus `current_score_date` (MAX score_date in gvm_scores), `score_date_moved`, and `preview_query` = the exact query string the saved rows resolve to.
- `GET /api/qb/universe2/defs[?basket_name=]` — every saved definition, newest first (the page's Load list).

**Page**

- A **Basket definition** card at the top: basket name, definition name (default 'default'), **Save**, **Load**, and a list of saved definitions. `?basket_name=X&def_name=Y` on the URL opens a definition on page load.
- A **NOTE** field on every applied row (typed in place, kept while typing — no re-render), a "✎ note" marker on the row head, and the notes shown again above the click-through results table ("Notes · Price change — momentum leg · Debt to equity (max) — no leverage").
- Save records the pools, the applied rows with AND/OR, duration (days or window), values and notes, plus the score_date and count of the last preview. The status line says what was saved.
- Load rebuilds the page state exactly (pool chips, rows applied and open, values, window, notes) and re-runs the preview. **On a later date it shows both dates**: "saved as of 2026-09-16 (40 stocks) · previewed now as of 2026-09-17 (40 stocks)" in amber. On the same date it says "(identical)" or flags a different count.
- An unknown name loads nothing and says so; the page does not change.

## VERIFY block

**1. Save with notes → reload in a fresh session → identical preview; notes present.**
Playwright harness (`scratchpad/cc2149_test.py`, dark + light at 1280 and dark at 375): Nifty 500 pool ·
price change 1M ≥ 5 (note "momentum leg") · D/E ≤ 1 (note "no leverage, banks excluded") → Save →
POST body carries both rows in page order with values, duration and notes, `as_of_score_date`
2026-09-16, `preview_count` 40 → page reloaded (empty state confirmed) → Load → the same two rows
applied and open, the same notes on the rows and on the results header, the pool chip on, and the
preview query string **byte-identical** to the one sent before the save. The page's `buildQuery()`
also equals the server's `_def_query()` for the same definition (JavaScript / Python parity), so
the `preview_query` in `/load` can be used to verify a reload without a browser. The preview endpoint
is deterministic (cc#2147: 40 on two runs), so an identical query gives an identical count.
Backend unit test: a 2-pool, 8-row definition (list rows, segments, a days row, a window row, a
K-of-N row, promoter, D/E) round-trips through `_clean_filters` into `_FILTER_ORDER` and renders the
expected query; seven bad payloads return 400 with the right message.

**2. UNIQUE upsert works; second save overwrites.** Run on the production table with the exact
INSERT … ON CONFLICT statement the endpoint uses, under a test name (then deleted):

| Save | pools | rows | result |
|---|---|---|---|
| 1st | 2 | 3 | id 1, `inserted` true, 05:05:49 UTC |
| 2nd | 1 | 2 | id 1, `inserted` false, `updated_at` 05:07:35 UTC |

Rows for the name after both saves: **1**. Second note read back: "no leverage, banks excluded".

**3. No ALTER anywhere.** The diff contains one `CREATE TABLE IF NOT EXISTS`; no ALTER, no index
change on an existing table.

Visual gate: screenshots looked at — definition bar dark and light, the D/E row with its note and
marker, the results header with the notes, the phone at 375 (bar wraps to three lines, no horizontal
overflow). Rendered-element counts: 2 note inputs on 2 applied rows, 2 markers, 1 saved definition in
the list.

## For the live check (the sandbox cannot reach scorr.in)

1. `https://scorr.in/qb/universe2` → pick Nifty 500, apply two rows, type a note on each, name the basket, **Save** → status "Saved … 2 applied rows, 2 notes … previewed as of <date> (N stocks)".
2. Open a new tab: `https://scorr.in/qb/universe2?basket_name=<name>` → the rows, notes and pool come back; the status line says "(identical)" on the same score_date.
3. `https://scorr.in/api/qb/universe2/load?basket_name=<name>` → `found` true, `preview_query`; paste it after `https://scorr.in/api/qb/universe2/preview?` → the same `count` the page shows.
4. `https://scorr.in/api/qb/universe2/defs` → the definition listed with `n_filters` and `n_notes`.

## Seen while reading, not changed here

Steps 2–4 persist at the table level only: `qb_entry_rules.save_ruleset`, `qb_exit_rules` and
`qb_risk_rules` have no HTTP route calling them, and `scorr_v12.html` carries no basket_name. The
universe step now has both. Wiring the three ruleset saves to the flow is a separate card.

## Not in this card

Deploying a saved universe to a live basket (the Deploy step, own card).
