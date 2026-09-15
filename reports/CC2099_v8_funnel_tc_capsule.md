# cc#2099 — V8 FUNNEL "Closest to Qualifying": GVM column replaced with a TC-score capsule

## Ground truth first (item 1)
`tc_universe_ticks.bucket` has exactly 4 real values — confirmed via DB: `BUY-REV`, `BUY-MOM`,
`SELL-REV`, `SELL-MOM`, all with 207 symbols ticked today (latest tick 15:20:59 IST, matching
TC_CANON_V2_FINAL's known last-tick time). The FUNNEL is already basket-scoped (`fnBasket`), so
each of the four `stock_passcount` handlers now reads its **own** basket's bucket — never a
best-of-four cross-side pick. Best-of-four (`mobile_home2.py:462-467`, cc#1936) was read and
confirmed deliberately side-agnostic for the Home breadth capsule, a sheet with no basket context
— a different case, not a precedent to copy here. Logged to `cc_task_logs` before wiring, per the
card's own verify requirement.

## Backend (`v8_endpoints.py`)
- `_TC_BUCKET_BY_BASKET` + `_tc_score100_for_basket(cur, basket)`: one new helper, mirrors
  `mobile_home2.py`'s proven `tc_universe_ticks` CTE read (same "latest tick today, IST" logic),
  narrowed to one bucket — no `DISTINCT ON` tie-break needed since there is only one bucket in
  play. A symbol with no tick today is absent from the returned dict, never fabricated as 0.
- `_registry_passcount(basket, rows, custom, tc_scores=None)`: one new parameter, one new field
  (`tc_score100`) added to the existing per-row dict. `gvm_score` itself — untouched, still read
  and still the sort key.
- All four handlers (`br_/sr_/sm_/bm_stock_passcount`) now fetch `tc_scores` once via the new
  helper and thread it through — `sr_`/`sm_`/`bm_` via the shared function's new parameter,
  `br_stock_passcount` (which predates that consolidation and keeps its own inline loop) via the
  same helper called directly and its own dict literal.

## Frontend (`mobile/v8.html`)
- `closestTable()`: the GVM column (`num(r.gvm_score) ? ... .toFixed(1) : '—'`, gold if ≥7.5) is
  replaced by `fnTcCapsule(r.tc_score100)`. Header cell relabelled `GVM` → `TC`. Everything else in
  the table (SYMBOL/MISSES chips, the passed-count bar, Day %) is untouched.
- `fnTcBand(score)` / `fnTcCapsule(score)`: client-side banding, same precedent as the deleted
  Home capsule's `breadthTcBand` (server ships the raw number, client buckets it) — STRONG ≥84,
  VALID ≥65, WATCH ≥50, else FAIL, per TC_CANON_V2_FINAL and cc#1958's locked treatment. No score
  today renders the same plain em-dash the GVM column used, same visual weight, no pill.
- CSS (`.fn-tc` + 4 band modifiers): STRONG = `--win` text on a 14% `color-mix` tint, weight 800;
  VALID = `--win` text, transparent fill; WATCH = `--muted`, weight 700; FAIL = `--loss` text,
  transparent fill. Border and text both ride `currentColor` so one rule serves every band.

**Token gate (item 4) — caught and fixed before shipping, not after.** The first version of `.fn-tc`
used bare `10.5px`/`1px 6px`/`4px`/`1px` (font-size/padding/border-radius/border-width) — zero raw
*colours*, but `theme_validator.count_raw` also tracks raw *lengths* on those same property
families, and a direct before/after run against `origin/main` showed a real **+4** regression, not
the 0 the card requires. Rewritten using the exact tokens `scorr_themes.css` already defines and
this same file already links (`--type-105`, `--space-1`, `--space-6`, `--radius-4`, `--bw-1` — the
last three reusing tokens `mobile/v8.html` itself already uses elsewhere, e.g. `.moodbar`'s
`border-radius:var(--radius-4)`). Re-verified: delta is exactly 0 against `origin/main`, and the
separate literal-fallback ratchet is unchanged (41 → 41) — this card added none.

## Verification
- `node --check` clean on the full extracted inline script.
- `ast.parse`/`py_compile` clean on `v8_endpoints.py`.
- **Raw-primitive ratchet**: computed directly with `theme_validator.count_raw` against
  `origin/main`'s actual committed content (not the MCP tool's cached snapshot, which read stale
  against a local, uncommitted edit) — before 138, after 138, delta **0**. Zero `.fn-tc*` rows
  flagged.
- **Real-data band check**: pulled real `tc_universe_ticks` scores for today's `BUY-REV` bucket
  and ran them through the actual committed `fnTcBand` (extracted verbatim, not retyped) —

  | Symbol | score100 (real, today) | Band |
  |---|---|---|
  | MAXHEALTH | 84.0 | STRONG (exact boundary) |
  | PAYTM | 80.0 | VALID |
  | YESBANK | 69.5 | VALID |
  | ADANIPORTS | 62.0 | WATCH |
  | ADANIENT | 54.5 | WATCH |
  | SHREECEM | 11.5 | FAIL |
  | RECLTD | 11.0 | FAIL |
  | SRF | 9.5 | FAIL |

  8/8 correct, including the exact 84.0 STRONG boundary. MAXHEALTH and PAYTM are two of the four
  symbols the founder's own screenshot named as missing from the narrower `v8_tc_score_ticks`
  table (evidence_numbers) — both present and scored here, confirming `tc_universe_ticks` is
  indeed the right, broader source.

## What did NOT change
`gvm_score`'s own computation and every other surface that reads it (marker-flag chips, the ≥7.5
gold rule elsewhere) — untouched, only this one column's display swapped. `tc_universe_ticks` and
the TC scorer itself — read-only. The C·A·R·D strip / A-button TC route — unrelated. Day % column,
passed-count bar, SYMBOL/MISSES chips in `closestTable()` — untouched.

## Precedent tension (flagged, per the card's own instruction, not silently resolved)
This is the same TC-score-as-a-badge idea cc#1974 removed from `/m/home` five days ago. That
ruling (cc#1975) is explicitly scoped to Home as a glance surface and explicitly does not address
`/m/v8`, which is already dense with numeric columns. Proceeding on the card's own stated basis;
flagged here again so it reads as a conscious, scoped call.

## Live check still needed (Arpit)
Capsule renders small, correct colour per band, dash for a symbol with no tick today (expected for
names outside the ~207-symbol daily-tick universe on a given day).
