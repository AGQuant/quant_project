# cc#2125 — mcap_rank per date from screener_raw: BUILD (follows the read-only diagnostic)

## Authority for the write
This card's own gate said: no first write until the founder replies GO. The founder's instruction
on 16-Sep-2026 (~15:50 IST), verbatim: *"if any blocker, decide yourself and ask claude in fable
room, I will verify after commit, dont hold anything for my call."* Treated as the GO — the gate
was purely "await founder GO", the ruling itself (rank from `screener_raw.market_cap`) was already
the founder's, and the decision was logged to `cc_task_logs` (task 2125) **before** any write.
Not on the live trading path: `qb_composite_select` / `qb_smallcap_select` compute rank fresh from
`screener_raw` themselves (verified in the diagnostic report), so no live basket buy/sell depends
on the column that was frozen.

## What changed since the diagnostic — read this first
`screener_raw` was reloaded **today**: `loaded_at = 2026-09-16 09:53:59`, 1,865 rows, one batch.
The 2026-09-08 batch the diagnostic measured is gone — the loader is a clean-replace, so nothing of
it survives. That is exactly the history loss a per-date table prevents, and it is why the only
honest historical point today is **2026-09-16**, not 2026-09-08 as the spec assumed (spec item 4:
"do NOT fabricate intermediate dates" — none were). The drift against `input_raw`'s frozen June
rank, re-measured on today's batch: **111 companies in the wrong band** (was 103 on 08-Sep),
20 enter / 26 leave the top 500 (was 14/23), average move 68.5 places, max 623, across 1,773
matched symbols. The number moved in a week — that is the point.

## What shipped
- **`mcap_rank_daily.py`** (new, rule 5): `CREATE TABLE IF NOT EXISTS mcap_rank_daily (symbol,
  rank_date, market_cap, mcap_rank, cap_category, source_loaded_at, computed_at, PK(symbol,
  rank_date))` + index `(rank_date, mcap_rank)`. `recompute_mcap_rank()` ranks `screener_raw`
  with `ROW_NUMBER() OVER (ORDER BY market_cap DESC, nse_code)` and writes ONE date (the batch's
  own `loaded_at::date`), replacing that date's rows only. Band edges are session_log 85's, in one
  SQL `CASE`: large 1-100 / mid 101-250 / small 251-1000 / micro 1001+. Two routes:
  `GET /api/qb/mcap_rank/status` (latest date, bands, and `rank_behind_screener` — true when a CSV
  landed that was not ranked) and `POST /api/admin/recompute_mcap_rank` (admin token, manual
  re-run without re-uploading a CSV).
- **`gvm_nightly.py`** (`_sql_clean_replace_screener_v2`, the one real writer to `screener_raw` —
  both loader paths, `admin_data.load_screener` and the v1 wrapper at line 1103, go through it):
  the recompute is hooked right after the cc#1865 post-load snapshot, same never-fail-the-load
  discipline, and its result rides back in the load's diagnostics dict as `mcap_rank_daily` so a
  rank that lags a CSV shows in the upload response itself. No separate schedule — spec item 1.
- **`qb_universe_builder.py`** (cc#2123's pool selector, the surface this card names as blocked):
  the four cap-band pools and the Nifty 500 pool now read `mcap_rank_daily` at its latest
  `rank_date` (`cap_category` for bands, `mcap_rank <= 500` for Nifty 500). Every rank-derived
  pool carries `ranked_as_of` and says "ranked as of 2026-09-16 (screener CSV load)" in its note on
  the page; the stale badge now fires only for a measured reason — a newer screener batch not yet
  ranked, or a rank older than 14 days — never by default. No HTML change was needed: the page
  already renders `note`, `stale`, `stale_note`.
- **`main.py`**: import + `include_router` only.

Tie-break: `screener_raw` had exactly one `market_cap` tie in today's batch; `ORDER BY market_cap
DESC, nse_code` makes it deterministic and gapless (0 duplicate ranks, span 1-1865, checked). This
matches the live selectors' own `ROW_NUMBER` derivation; the diagnostic's `RANK()` could differ by
at most that one tie.

## First-run evidence (rule 9 — the badge follows the data)
Written 16-Sep-2026 via the identical `INSERT ... ROW_NUMBER()` statement the module carries:
- `rank_date = 2026-09-16`, **1,865 rows**, 1 date stored, `rank_date == screener loaded_at::date`
  (not behind).
- Bands: **large 100 / mid 150 / small 750 / micro 865** — exactly the session_log 85 edges.
  Top-500 = **500**. Zero duplicate ranks.
- **BAGMANE now has a rank: 243, mid** (it had none in `input_raw` — "new to coverage, never
  ranked", per the diagnostic).
- Named movers on today's batch: LENSKART 85 large, LODHA 100 large (right on the edge), VEDL 113
  mid, INDUSTOWER 115 mid, TATACONSUM 116 mid, PATANJALI 234 mid (it read small on the 08-Sep
  batch — it moved back; a per-date rank is what makes that visible instead of silently wrong),
  LTTS 253 / PIIND 258 / HUDCO 260 / UBL 271 / IREDA 286 / GODFRYPHLP 289 small.
- The exact pool SQL strings the code now uses, run against the real table: cap_large 100 raw /
  100 scored, cap_mid 150 / 147, cap_small 750 / 734, cap_micro 865 / 789, nifty500 500 / 490
  (scored = has a current `gvm_scores` row, the count the page shows). Compare cc#2123's stale
  counts of 100 / 149 / 748 / 761 / 500.
- **Honest limit:** the loader HOOK has not yet fired end-to-end — it runs on the next screener
  CSV upload. The first write above was done manually through the same SQL, not through the hook.
  When the founder next uploads, the load response's `mcap_rank_daily` key and
  `/api/qb/mcap_rank/status` (`rank_behind_screener` must be false) are the proof it works; if the
  key reads `status: error`, that is the bug to chase, and it will be visible, not silent.

## Spec item 7 — nifty500_universe retirement, stated
For the Universe page, `nifty500_universe` is retired: the Nifty 500 pool is now a query over the
per-date rank (one ranking, one source). The table is **not dropped** and its other readers are
**not** re-pointed on this card: `qb_alpha_select.py` (Alpha Multicap's live universe — changing it
re-selects a basket, which this card's own do_not_touch forbids: "no basket is re-selected on this
card, even after the rank is fixed"), and `qb_replay.py` (replay approximation). Those are a
separate founder decision.

## Not cut over on this card — proposed follow-up (Fable Room)
Readers still on `input_raw.mcap_rank` / `cap_category`, all display or filter surfaces, none a
trading decision: `native_router.py` (2 sites, app cap-band filter), `qb_app_mobile.py` (display),
`gvm_market_endpoints.py` (display), `hr_report.py` (one median-PE aggregate), `v12_endpoints.py`
(protected by cc#2123's own do_not_touch), `worker/fyers_feed.py` (Hard Line; orders the feed
rollout, has its own missing-rank fallback). A single follow-up card can re-point the five
unprotected ones to `mcap_rank_daily` at latest `rank_date`. Until then they show June's bands —
stated here so it is a known gap, not a hidden one.

## Verify (spec checklist)
- Drift counts named, not percentages — yes (above, re-measured on today's batch).
- `scheduler_master` — no rank job exists and none was added: the recompute is event-driven on
  the CSV load by the spec's own instruction (item 1). `to_regclass` guards the status route.
- BAGMANE cause stated (diagnostic) and now resolved by the write (rank 243).
- No ALTER on `input_raw`; new table is additive; `input_raw` columns untouched.
- `ast.parse` + `py_compile` clean on all four Python files.
- No basket re-selected; `quant_basket` / `quant_paper_positions` untouched.

## What did NOT change
`input_raw` (schema and data), `nifty500_universe` (table and data), every basket selector, the
band edges, the Universe page HTML, the futures pool.
