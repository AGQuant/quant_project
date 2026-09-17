# cc#2145 — QB Universe builder: CAT_2 Sector & Segment wired (5 locked rows), in-query, no job

Step 3 of the QB Universe sprint. Five rows, all window functions over the rows the CAT_1 query
already reads. No table, no job, no scheduler row (ruling 2 — server load).

## The one deviation, stated up front: sector metrics are UNIVERSE-wide, not pool-wide
The card's item 2 says "sector_member_count … (already emitted as seg_size)". `seg_size` (and
`seg_rank`, `gap_vs_sector`) are POOL-scoped — the founder's rank-within-segment — and stay
exactly as cc#2123 wrote them. The four new sector columns are computed over the **whole scored
universe** at the latest score_date and joined by segment. Reason: the card's own verify asks that
`sector_gvm` equal `sector_ratings.mcap_weighted_gvm` (a universe number) and that the member count
gate agree with N5_SEGMENT_GATE_V1 (a universe rule); a pool-scoped weighted mean would change every
time the pool changed and match neither. Flagged for Arpit/Fable; a pool-scoped variant is one
more window column if ever wanted.

## Backend (`qb_universe_builder.py`)
- `_UNI_SECT_SQL` (CTEs `latest → uni → sect → sect_ranked`) mirrors `compute_sector_ratings`
  (`gvm_nightly.py`) exactly: `gvm_score IS NOT NULL` rows only; segment `''`/`Unknown`/NULL
  skipped; **mcap-weighted mean over members with a market cap** (cc#1104: a missing market cap
  is an exclusion from the weight, never a fake weight); a segment with no market cap at all falls
  back to the simple mean; rounded to 3 dp like the table. `sector_rank` = `DENSE_RANK()` over
  segments by the unrounded weighted mean, DESC. `sector_member_count` = every scored member.
- `_SCORED_CTE` = pool + that sector block + the CAT_1 `scored` columns (unchanged) +
  `sector_gvm`, `sector_rank`, `sector_member_count`, `gvm_minus_sector` (= `gvm_score − sector_gvm`,
  2 dp) via `LEFT JOIN sect_ranked ON segment`. `_CAT1_SQL` and every count (total, alone, step)
  now read this ONE CTE, so a count and a row can never disagree.
- Five filters, appended to `_FILTER_ORDER` after the CAT_1 rows (so the cc#2144 funnel runs
  top-to-bottom through both blocks): `sector_gvm_min/max`, `sector_rank_min/max`,
  `sector_gap_min/max`, `sector_members_min/max` (the page sends min only), `segments=` repeated
  → `segment = ANY(list)`. `_combine` / `_parse_ops` untouched.
- Item 5: `segment_stats {segments, min, median, max}` — the POOL's segments and their member
  counts (one extra query on the same CTE), shown beside the Rank-within-Segment row, never
  auto-applied. cc#2123 had not surfaced it; it is here now.
- New `GET /api/qb/universe2/segments`: every segment with members, sector GVM (3 dp) and rank,
  from the same CTE — the list the Segments multi-select reads.

## Page (`scorr_qb_universe.html`)
- "Sector & Segment — Coming" is replaced by a live block with the CAT_1 row pattern: `+` opens a
  row, min/max (whole numbers for rank and count; Member Count is min-only, "and above"), AND/OR
  flag, alone count, step count (cc#2144), tick-to-apply. Segments is a multi-select: picked
  chips, a search box, a scrollable list where every segment shows `members · GVM · #rank`.
- **POINT-IN-TIME** (green) data-backing badge on the block header and on every CAT_2 row.
- Header now reads "n active of 11 live · 40 in the locked set" — 11 rows are wired, the locked
  V1 set is 40; the five other categories stay "Coming".
- Result table gains the applied CAT_2 columns (Segment never duplicated).

## Verify (the card's own list)
1. **sector_gvm vs sector_ratings**: all **126 segments compared, 0 unmatched, max |diff| 0.000**,
   member counts identical. Three named: IT - Small 6.594 (rank 26, 32 members) · Pharma -
   Formulations 6.982 (rank 5, 15) · Shipping & Maritime 7.262 (rank 1, 7) — page = /sector.
2. **member count ≥ 5**: today no segment has fewer than 5 members (min 5, median 13, max 40), so
   the gate removes **nothing** — exactly what N5 would gate today (nothing). The three segments
   sitting AT 5 (Garments & Apparel, Integrated Steel - Large, Telecom Equipment & Services) are
   kept by "≥ 5" and would be the first to go under "> 5".
3. **segments in (IT - Small, Shipping & Maritime)** on Nifty 500 → 2 rows, both in exactly those
   two segments (39 in the universe); alone + step counts flow through the row (harness).
4. **Same-day determinism**: the count for Nifty 500 · GVM ≥ 7 · sector rank ≤ 20 · members ≥ 5
   was **42 on both runs** (EXPLAIN run and a plain count minutes later) — one EOD snapshot.
5. **EXPLAIN ANALYZE** of the final preview query (that setup, Nifty 500): **execution 5.1 ms,
   planning 2.1 ms**, 445 shared buffers; the universe pass is a 1,773-row scan of the single
   score_date, the pool join index-bound on `mcap_rank_daily`. No cache needed.
- Fake-DB unit test of the endpoint: order `gvm → sector_rank → sector_members → segments`,
  expression `(((gvm AND sector_rank) AND sector_members) OR segments)`, params bound, the
  scored CTE with `sect_ranked` in the executed SQL, `segment_stats` in the payload.
- Playwright on the real page (dark 1280 / light 1280 / dark 375; `/pools`, `/segments`,
  `/preview` stubbed with the measured numbers): 5 rows in order, 6 badges, 5 "Coming" left,
  header text, whole-number steps on Sector Rank, min-only Member Count, segment list + search
  ("pharma" → 2), picked chips, request `gvm_min=7&sector_rank_max=20&sector_members_min=5&
  segments=IT - Small&segments=Shipping & Maritime`, rank row "214 of 490 pass on its own | 42
  remain after this step", context line "Segments in this pool: 88 · members min 1 · median 5 ·
  max 18" (real Nifty 500 stats), result columns, no overflow, no page errors; `node --check`
  clean; 0 literal fallbacks. Screenshots looked at (VISUAL_VERIFY_GATE_V1).

## Not touched
CAT_1 row semantics; the pool section; `sector_ratings`, `gvm_nightly`, `compute_sector_ratings`;
`v12_endpoints.py` / `scorr_v12.html`. Out of scope as listed: the two universe_technicals
sector-return rows; saving definitions (cc#2149).
