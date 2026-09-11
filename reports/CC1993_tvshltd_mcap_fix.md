# cc#1993 — TVSHLTD market cap 47x fix + loader guard + dominance logging

Items 1-3 landed this push. Item 4 (recompute Castings & Forgings, post before/after) is
deferred to after 15:30 IST per this card's own `window` field (`"recompute after 15:30 IST
preferred"`) — same discipline this session has applied to every other live-scoring recompute
today (cc#1996, cc#1999).

## Item 1 — the row, corrected from a sourced number

`screener_raw.market_cap` for TVSHLTD (TVS Holdings) was `12,64,711.73` Cr. Five external feeds
on 09-Sep-2026 (per cc#1985's finding, Fable-sourced, log 6301 RULED 2) put it near Rs 27,500 Cr —
Kotak 27,064; three others 27,732 / 28,320 / 28,843. Share-count cross-check in the same finding
(~2.02 Cr shares x ~Rs 13,100 CMP = ~Rs 26,500 Cr) lands in the same neighbourhood. ~46x too high,
confirmed independently here against the stored `price` (13,300) and share-count arithmetic.

Written as **Rs 27,500.00 Cr**, the spec's own midpoint figure — not re-derived from the four
feed numbers visible in the finding, which don't by themselves average to exactly 27,500 (a fifth
feed value is referenced but not quoted in the source finding); the spec states the number to use
and this follows it rather than silently recomputing a different one.

`pe` recomputed from the corrected market cap against the stored TTM profit (`Profit after tax`
= 1,987.61 Cr, unchanged): **636.11 -> 13.84**. (`historical_pe`, a separately-stored field, is
untouched — the card asks only for the PE paired with market cap.)

No `note`/`remarks` column exists on `screener_raw` (checked the live row's full column list
before writing) — the spec's fallback applies: the source list is stated here, not in the row.

Verified by reading the row back after the write, not assumed from the UPDATE's row count:

```
nse_code | market_cap | pe
TVSHLTD  | 27500.00   | 13.84
```

## Item 2 — the loader guard, symmetric, disclosed as wider than the literal spec line

Traced the actual write path first rather than guessing: `POST /api/admin/load_screener_from_drive`
(`admin_data.py`) calls `_sql_clean_replace_screener_v2()` in `gvm_nightly.py` — **not**
`screener_loader.py`, which is a separate, unwired standalone CLI scorer that never touches
`screener_raw`. The real loader is a clean-replace (`DELETE FROM screener_raw` then a fresh
`INSERT`), so catching a bad value means reading the *prior* per-symbol market_cap before the
delete — there is nothing left to compare against or fall back to afterward.

Added: before the delete, read `{nse_code: market_cap}` from the live table. While building each
row's insert values, compare the incoming market_cap against that symbol's prior value. If it
moves **>10x in either direction**, reject just that one cell — keep the row's other fresh fields
(price, PE, fundamentals, …), substitute the *previous* market_cap, log every rejection with its
ratio, and surface the full list in the endpoint's JSON response
(`market_cap_jump_guard_rejections`) the same way `columns_dropped_populated` already surfaces the
cc#828 guard's findings.

**Disclosed, not silent:** the spec's literal line is "jumps >10x" (upward — matches the TVSHLTD
case). Implemented symmetric (also catches a >10x *drop*) because the same root cause — a units or
consolidation slip at source — divides as easily as it multiplies, and guarding both directions is
the same few lines of code. Narrow it to upward-only on a one-line ask if that's not wanted.

Guard logic verified with four cases run in isolation (no DB touched — a live clean-replace
against 1,881 real rows is not something to rehearse with synthetic data just to test a guard):
the old bad TVSHLTD value (12,64,711.73) re-imported against the now-corrected baseline (27,500)
trips the guard (45.99x); a normal day-to-day move (27,500 -> 27,812) does not; a symmetric
downward 13.75x slip trips it; a 9.8x move — just under the line — correctly does not. All four
passed.

`market_cap` was already a live numeric column (not `screener_loader.py`'s allowlist path), so
this needed no `ALTER TABLE` and touches only `_sql_clean_replace_screener_v2`'s Python logic —
nothing under `MAINTENANCE_LOCK_RULE`.

## Item 3 — dominance logging in `compute_sector_ratings`, reporting only

Added a `dominant` list, built the same way `excluded` (cc#1104) already is: for each segment,
while `total_mcap` still holds the real weighted sum (read *before* the equal-weight fallback
branch can overwrite it for a segment with zero market-cap coverage), any member whose own
market_cap exceeds 50% of that segment total is appended with its weight%, logged via
`log.warning` the same way `excluded_no_market_cap` logs, and returned in the payload as
`dominant_segment_members`.

**No formula change** — `wt_avg`/`simple_avg`/the `rows` construction/the `INSERT` are untouched.
The card is explicit that dominance itself is not a defect: RELIANCE (~63% of its segment), TITAN
(~74%), ASIANPAINT (~72%) are real market structures and must never be capped. This exists only so
a case like TVSHLTD — a *wrong* number dominating a segment — is visible in the log and the
response before it ships, instead of being discovered after the fact the way this card started.

## Verify

- `SELECT nse_code, market_cap, pe FROM screener_raw WHERE nse_code='TVSHLTD'` — read back above,
  shows 27,500.00 / 13.84 as required.
- Committed diff: this push, `gvm_nightly.py` only (loader guard + dominance logging).
- Guard rejects the old bad row on a synthetic re-check (above) — not run against the live table.
- Item 4 (Castings & Forgings before/after) intentionally **not** run this tick — recompute window
  is after 15:30 IST per the card, and today's other live-scoring recomputes are queued for the
  same window.

`ast.parse` + `py_compile` clean on `gvm_nightly.py`. Nothing under `worker/**`. Formula in
`compute_sector_ratings` unchanged — confirmed by inspection, not just by intent. Card **NOT**
set done — Fable verifies, per standing rule. Finding row posted to cc#1199 per this card's own
`finding_rule` (DIAG_FINDINGS_SURFACE_V1).
