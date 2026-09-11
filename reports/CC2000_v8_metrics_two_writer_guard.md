# cc#2000 — v8_metrics two-writer COALESCE guard + V13 "2-day high" label fix

## Item 1 — the guard, applied to 14 columns, not the card's "eleven"

Read the full `store_metrics()` upsert in `v8_engine.py` (the EOD writer) before touching
anything, rather than guarding only the columns the card names. Found **14** bare
`col=EXCLUDED.col` overwrites, not eleven — stating the discrepancy rather than quietly matching
the title. The eleven the card names (three DMAs, three RSIs, three returns, eod_chg) are all
real; grep also turned up four more of the exact same shape the card's own scope line anticipated
("and any other bare overwrite you find — list them all"): `month_index`, `week_index_52`,
`ma9_vs_ma21`, `vol_ratio`.

Full list, with what each was before this push (all in the same `ON CONFLICT (symbol,
score_date) DO UPDATE SET` block, `v8_engine.py` ~line 330-345):

| Column | Was |
|---|---|
| dma_50, dma_200, dma_20 | bare `EXCLUDED.col` |
| rsi_month, rsi_weekly, daily_rsi | bare |
| month_return, week_return, year_return | bare |
| eod_chg | bare |
| month_index, week_index_52 | bare |
| ma9_vs_ma21, vol_ratio | bare |

All 14 now read `col=COALESCE(v8_metrics.col, EXCLUDED.col)` — the identical direction and
pattern `mom_2d`/`day_1d`/`sector_day`/`sector_week`/`sector_month` already use (cc#1461):
**prefer whatever is already in today's row, fall back to the EOD recompute only if nothing
live claimed the column.**

Why this direction is safe for all 14, not just the five that already had it: `score_date` is
part of the `ON CONFLICT` key, so this UPDATE branch only ever fires when a row for **today's**
date already exists — meaning `v8_signal_writer` already touched today's row at some point before
this 15:45 EOD pass ran. There is no multi-day freeze risk (each trading day starts a fresh row);
the guard only ever decides "today's live value" vs. "today's EOD recompute," never "forever vs.
now." Confirmed against the CLAUDE.md V8 lock cited directly on the card: *"EOD frozen: gvm_score
only ... Live every 5-min: all 19 other metrics via v8_signal_writer"* — that line is the
platform's own statement that every column but one is conceptually live-owned, which is exactly
what all 14 guards now enforce uniformly instead of five doing it and nine not.

## Item 2 — gvm_score stays bare, on purpose, now says so in the code

`gvm_score=EXCLUDED.gvm_score` is untouched, first line of the SET clause, with a comment
directly above it citing the same CLAUDE.md lock as the reason it — alone — stays a plain
overwrite.

## Item 3 — the mislabel, verified against source before changing anything

Read `v8_signal_writer.py:923-929` first rather than trusting the card's characterization on
faith: `fall_from_day_high = (live - today_high) / today_high * 100`, comment confirms `today_high
= fyers_eq day high (bar["high"])` — genuinely same-day, not a 2-day window. `scorr_v13.html:238`
labeled it *"Fall off the 2-day high %"*, which describes a different, real metric that exists
elsewhere in the codebase (`tc_v4_dual.py:255-261`'s R10, `fall_from_high_2d`, explicitly built
from `daily[-2:]` — genuinely 2-day). The V13 label was borrowing a name that belongs to a
different field.

Changed to **"Fall from today's high %"** (the card's exact requested wording).
`tc_v4_dual.py` untouched, per `do_not_touch` — confirmed its own label ("fall from 2-day high")
stays textually distinct from the corrected V13 one, so the two no longer collide in either
direction (V13 no longer borrows R10's name; R10 was never touched to begin with).

`grep "2-day high"` across `mobile/` and `*.html` after the edit: only the genuine R10 reference
remains (`tc_v4_dual.py` is `.py`, not matched by the html glob, and was not touched anyway).

## Item 4 — untouched, confirmed

`main.py:1991`'s dead handler not opened. `v8_signal_writer.py`'s write path not modified — only
read, to verify item 3. `tc_v4_dual.py`'s R10 formula not modified — only read, same reason.

## Verify

- Diff shows COALESCE on all 14 non-gvm_score columns — `git diff` this push, `v8_engine.py`
  only for item 1/2 (plus `scorr_v13.html` for item 3).
- DB verify (5 symbols, 15:20 tick vs post-EOD dma/rsi/return columns unchanged or non-null,
  gvm_score may change) needs **the next 15:45 EOD run** — cannot be verified before it happens;
  will report it after today's EOD run, not before.
- `grep "2-day high"` across `mobile/` and `*.html`: only R10 remains, confirmed above.

`ast.parse` + `py_compile` clean on `v8_engine.py`. `node --check` clean on both inline script
blocks of `scorr_v13.html` (32,330-char block containing the edit passes). `v8_engine.py` is not
under `worker/**` (confirmed by path) — pushed now, not held for 15:30. Nothing in
`v8_signal_writer.py`, `tc_v4_dual.py`, or `main.py` touched. Card **NOT** set done — Fable
verifies, and item 1's DB verify specifically needs tonight's 15:45 run before anyone can close it.
