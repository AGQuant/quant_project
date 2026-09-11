# cc#1993 item 4 — Castings & Forgings recompute, before/after

Per Fable's order (log 6354, item 4, second push). No new recompute run for this alone — the
`gvm_recompute` already run as part of cc#1996's segment-move push (same tick, this report
written right after) already carries this segment's post-fix state; re-running again would just
reproduce the identical numbers against the same live data.

## Before → after

| | Before (card's own evidence) | After (live now) |
|---|---|---|
| `mcap_weighted_gvm` | 6.268 | **6.886** |
| Members | 17 (TVSHLTD + 16) | 18 |
| Top stock | TVSHLTD (was distorting the weighting) | PTCIL, 8.30 |

The card's verify line targeted "toward 6.87" (the evidence's own "other 16 members give 6.874"
figure) — the live number, **6.886**, lands essentially there. Not identical because the segment
gained 3 new members since the card was written: `NELCAST`, `STEELCAS`, `BALUFORGE` all moved
INTO `Castings & Forgings` in the same tick's cc#1996 batch (149 F-class moves), so this is the
17-original-member figure plus three real new members' own scores blended in, not a mismatch.

## Does `dominant_segment_members` fire for this segment? No — and that is the correct answer.

`Castings & Forgings` does not appear in either of today's two `dominant_segment_members` runs
(the guard built in cc#1993 itself, sha `bfc52fb`). Checked directly: 18 members, total mcap
Rs 3,42,027 Cr, largest single member `BHARATFORG` at Rs 94,446 Cr = **27.6%** of the segment —
nowhere near the 50pct threshold. `TVSHLTD` now sits at Rs 27,500 Cr (the corrected value),
**9.7%** of segment weight, a normal-sized member rather than the ~370%-of-segment distortion the
uncorrected 12,64,712 Cr figure would have produced. This is the guard confirming, on live data,
that the fix actually worked — a segment that WAS being distorted by one bad number is now a
genuinely diversified 18-member segment with no single dominant holder.

## Verify

- `sector_ratings` row for `Castings & Forgings`, read live: `mcap_weighted_gvm=6.886`,
  `stocks_count=18`, `top_stock=PTCIL`.
- `gvm_scores` for the segment: 18 rows, `TVSHLTD` present at the corrected market cap and a
  reasonable score (6.86), not an outlier.
- `dominant_segment_members` from today's recompute: `Castings & Forgings` absent from the list,
  confirmed by direct query (largest member 27.6% of segment weight).

No code changed, no new DB write beyond what cc#1996's push already made — this report documents
the already-live state. Card **NOT** set done — Fable verifies.
