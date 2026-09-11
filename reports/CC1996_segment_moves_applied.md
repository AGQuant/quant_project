# cc#1996 items 2-4 / cc#1985 item 5 — 149 F-class segment moves APPLIED

Landed the first after-15:30 IST push today, per the card's own window rule and Fable's order
(log 6354, item 4). Source: `reports/CC1996_filing_error_split.md` (sha `c94796e`), the corrected
149-row F table — no further correction needed here, the moves were applied exactly as listed.

## Batching

4 batches, alphabetical, **with one deliberate exception**: `MIDHANI` (alphabetically far down
the list) was pulled forward into batch 1 alongside `AZAD` (already there alphabetically), so the
`Defence PSU` net effect — `MIDHANI` moving in, `AZAD` moving out — resolved inside ONE batch, not
split across two. The report's own warning named this exact risk ("a mid-batch read could see a
transient wrong count"); batching code + the resulting groups are in this push's commit for
anyone who wants to re-derive them.

| Batch | Symbols | Notes |
|---|---|---|
| 1 | 40 | Includes AMAGI (founder-named, per the report) and MIDHANI+AZAD together |
| 2 | 40 | |
| 3 | 40 | |
| 4 | 29 | |
| **Total** | **149** | matches the report's F count exactly |

## Per-batch discipline (all 4 batches)

Each batch: (1) `UPDATE input_raw SET gvm_segment=... WHERE nse_code=...` for that batch's
symbols only, with `RETURNING` checked against the expected row count every time (40/40/40/29,
all matched — no batch silently updated fewer rows than intended), (2)
`SELECT gvm_segment, COUNT(*) FROM input_raw GROUP BY gvm_segment HAVING COUNT(*) < 5` — the
N5_SEGMENT_GATE_V1 check, run fresh after every batch, not just once at the end.

**N5 gate: clean after every single batch, and clean at the end.** Zero segments dropped below 5
members as a result of these moves. The only two segments currently under 5 are `Plastics and
Packaging` (3) and `Textiles Smallcap` (4) — pre-existing, already known, and explicitly out of
this card's scope (they are cc#1999's own subject, not touched here).

## Recompute

`mcp__Scorr__gvm_recompute(refresh_momentum=false)` — momentum untouched on purpose, only the
segment reclassification needed to flow through. Run **twice**: once after batch 1 (to catch any
gate issue as early as possible rather than waiting for all 149), once after batch 4 (final
state). Both runs: `status: ok`, `scored: 1793`, `errors: 0`, `sector_ratings: {status: ok,
segments: 128}`, `thinned_segments: []`.

**First real firing of cc#1993's `dominant_segment_members` logging (built earlier today, sha
`bfc52fb`) — reporting the real numbers as promised.** Final recompute surfaced 16 segment
members above 50pct of their segment's weighted market cap, every one a genuine large-cap
structural case, not a data error: RELIANCE 64.7% (Refineries & Exploration - Large), ADANIENT
83.7% (Diversified), TITAN 75.9% (Gems & Jewellery - Large), ASIANPAINT 71.6% (Paints),
BHARTIARTL 66.9% (Telecom Services), LT 62.0% (Engineering - Large), PIDILITIND 73.9% (Adhesives,
Coatings & Polymers), CUMMINSIND 55.4%, LGEINDIA 69.3%, PWL 69.9%, AFFLE 56.1%, LAURUSLABS 60.0%,
KNACK 65.8% — plus two members that are dominant precisely *because* of this push's own moves,
correctly so: **POWERGRID 83.2%** of `Power Services & Trading` (POWERGRID moved into this
segment in batch 3; a PSU of its size dominating a smaller segment it just joined is exactly the
"real structure" the logging is meant to surface, not hide) and **WAAREEENER 63.7%** of
`Electrical Equipment Small` (batch 4). No capping applied anywhere — reporting only, per the
card's own rule. TVSHLTD does **not** appear in either run's list, confirming cc#1993's fix holds.

## Final verification — every one of the 149, both tables

```
total_moves           149
input_raw_correct     149   (gvm_segment matches the planned destination)
gvm_scores_correct    149   (segment matches the planned destination, post-recompute)
gvm_scores_matched    149   (every symbol has a live score row -- none dropped out)
```

All four numbers equal 149. Nothing missing, nothing landed on the wrong destination.

## What did NOT move here, stated plainly

- **PASUPTAC** — not in this batch. It is one of `Textiles Smallcap`'s 4 members, so cc#1999 (the
  sub-5 merge) resolves it directly rather than this card writing it twice, per the source
  report's own note.
- **TVSHLTD, CUPID** — held out per the source report. TVSHLTD is cc#1993's row (already
  corrected, sha `bfc52fb`); CUPID carries the same inflated-mcap signature but has no founder
  ruling of its own yet.
- **The 27 S-class rows and 18 X-flagged rows** — untouched, per the card's own split; they need a
  size-tier design decision or remain genuinely undecided, neither of which this push resolves.
- **JYOTICNC** — in the S list (size-tier hold), not F; its own would-be exit from `Defence PSU`
  did not happen in this push, so `Defence PSU`'s net change here is MIDHANI in / AZAD out only
  (not the three-way net the source report's cross-batch note described, since JYOTICNC isn't
  part of what actually got applied).

## Verify

- Batch UPDATE row counts: 40/40/40/29, all matched via `RETURNING`.
- N5 gate: `SELECT segment, COUNT(*) FROM input_raw/gvm_scores ... HAVING COUNT(*) < 5` — clean
  after every batch and at the end (only the two pre-existing, cc#1999-owned segments).
- `gvm_recompute`: `errors: 0` both runs.
- Final cross-check query above: 149/149/149/149.

No code file changed — this is a data operation (`input_raw` + a `gvm_recompute` call), not a
code push; this report is the artifact. `worker/**` untouched. Card **NOT** set done — Fable
verifies.
