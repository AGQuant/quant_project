# cc#1999 items 2-4 — N5_SEGMENT_GATE_V1: merge applied, guard landed, registry retired

Item 1 (destination research) was already done and reported separately:
`reports/CC1999_destination_research.md`, sha `c4227b4`. This report covers items 2-4.

## Item 2 — the 7-symbol move, applied

Per the card's own evidence (`Plastics and Packaging` n=3: HITECHCORP, KANPRPLA, KNACK;
`Textiles Smallcap` n=4: LAKSHMIMIL, NAHARINDUS, ORBTEXP, PASUPTAC) and item 1's destination
research, `input_raw.gvm_segment` updated for all 7, each confirmed via `RETURNING`:

| Symbol | Destination |
|---|---|
| HITECHCORP, KANPRPLA, KNACK | Flexible Packaging & Films |
| PASUPTAC, NAHARINDUS, LAKSHMIMIL | Synthetic Fibres & Yarn |
| ORBTEXP | Home Textiles & Technical |

`gvm_recompute` run after the move (`refresh_momentum=false` — only the reclassification needed
to flow through, momentum untouched on purpose): `errors: 0`, `segments: 126` (down from 128 —
both source segments correctly dropped to zero members, nothing left behind under either name).

**Per-symbol GVM, before -> after:**

| Symbol | Before | After | Δ |
|---|---|---|---|
| HITECHCORP | 5.65 | 5.68 | +0.03 |
| KANPRPLA | 7.17 | 7.17 | 0.00 |
| KNACK | 5.85 | 6.09 | +0.24 |
| LAKSHMIMIL | 4.97 | 5.00 | +0.03 |
| NAHARINDUS | 6.59 | 6.82 | +0.23 |
| ORBTEXP | 6.28 | 6.88 | +0.60 |
| PASUPTAC | 7.61 | 7.61 | 0.00 |

Max absolute delta 0.60 (ORBTEXP) — small, in-segment reshuffling, no wild swing. "After" column
re-confirmed live just now (this push), not carried from memory: `SELECT symbol, segment, gvm_score
FROM gvm_scores WHERE symbol IN (...)` returns exactly these 7 values.

**Destination segment stats, live now** (`gvm_scores`, scored/live universe — the number GVM
displays actually use):

| Segment | Members | Avg GVM |
|---|---|---|
| Flexible Packaging & Films | 18 | 6.386 |
| Home Textiles & Technical | 14 | 6.136 |
| Synthetic Fibres & Yarn | 27 | 6.073 |

(`input_raw` shows Synthetic Fibres & Yarn at 31 raw-assigned members vs 27 scored — a normal,
pre-existing gap: some input_raw rows have no matching screener_raw fundamentals row and so never
reach gvm_scores, same as everywhere else in the universe. Not something this card touches or
needs to close.)

**N5 gate, live now:** `SELECT segment, COUNT(*) FROM gvm_scores WHERE segment IS NOT NULL GROUP BY
segment HAVING COUNT(*) < 5` → **zero rows**. Both `Plastics and Packaging` and `Textiles Smallcap`
are gone from gvm_scores entirely — not thinned, not present at all.

## Item 3 — the guard, `gvm_nightly.py`

**Where it lives, and why not inside `_peer_averages` itself.** `_peer_averages(df)` has 3 external
callers (`gvm_nightly.py:934` its own use, `gvm_backfill.py:195`, `gvm_inputs.py:107`) that all
depend on its `Dict[str, Dict]` return shape (segment -> peer-median dict). Item 3's scope is
`gvm_nightly.py` only — changing `_peer_averages` itself would silently reach into the other two
files' behavior, outside this card. So `_peer_averages` is **byte-for-byte unchanged**. A new
function, `_segment_gate(df, peer_avgs)`, runs immediately after it and does the actual guard work
as a pure post-process: same input, same dict identity back out (mutated only for the borrow case),
plus a `gated_segments` set and an `under5` report list.

**The two outcomes, exactly per the card's wording** ("scored against the nearest related segment
if a mapping exists, else NOT scored and logged"):
- **Mapped** (`SEGMENT_FALLBACK_MAP[seg]` names a real segment): that segment's own peer averages
  are substituted in for the thin segment, so its members keep scoring normally against a real
  peer set instead of their own 2-4-member one.
- **Unmapped** (the default — `SEGMENT_FALLBACK_MAP` starts **empty**): the segment is added to
  `gated_segments`. `recompute_gvm`'s own loop checks this before calling `_stock_dict`/scoring for
  each row and `continue`s past any row whose segment is gated — **no `gvm_scores` row is written
  for these symbols this cycle at all.**

**Why "NOT scored" means excluded from `gvm_scores`, not a soft null-out of peer-relative inputs
only.** The card's own verify line is explicit: `SELECT segment, count(*) FROM gvm_scores ...
HAVING count(*)<5` must return **zero rows** after recompute. A design where a gated member keeps
a `gvm_scores` row under its own tiny segment name (only its peer-relative components going null)
cannot satisfy that — the segment would still appear, still under 5. So the guard hard-excludes:
no row, no count, verify passes by construction for any future unmapped sub-5 case, not just
today's already-fixed two.

**Why `SEGMENT_FALLBACK_MAP` starts empty.** Item 1 of this same card put it plainly: "Fable does
not pre-pick — you have the taxonomy in front of you." Picking which of dozens of segments is
"nearest related" to a newly-thin one is a business judgement, the same kind item 1 exercised by
hand for these exact two segments. The guard does not invent that judgement unasked — it logs the
sub-5 case and leaves the map empty until a human (or a future card) states the mapping explicitly,
or — the better fix, per item 2's own example — the segment gets merged for real in `input_raw`.

**Return payload.** `recompute_gvm`'s result now carries `segment_under_5` (every thin segment this
cycle, its member list, and which action was taken) and `segment_gated_rows` (which symbols got no
`gvm_scores` row because of it — empty in the normal case). Same style as `compute_sector_ratings`'s
existing `excluded_no_market_cap` / `thinned_segments` / `dominant_segment_members` — reported with
the result, not buried in a log line only.

## Synthetic test — proving the guard fires, per the card's own verify requirement

`fastapi` / `psycopg` are not installed in this environment, so a direct `import gvm_nightly` isn't
possible; `pandas`/`numpy` were installed into a scratch venv and the four unavailable/side-effecting
imports (`fastapi`, `psycopg`, `gvm_engine`, `momentum_daily`) were stubbed via `sys.modules` so the
**real, just-edited `gvm_nightly.py`** loads and its real `_segment_gate` / `_peer_averages` /
`SEGMENT_FALLBACK_MAP` / `MIN_SEGMENT_MEMBERS` are exercised directly — not a reimplementation.

Three cases, all against the real functions, log output captured from `gvm_nightly.log` itself:

1. **Synthetic 3-member segment, no fallback** (6-member control segment alongside it): logs
   `N5_SEGMENT_GATE_V1: segment_under_5 'Synthetic Thin Segment Case1' (3 members: THINSEG0,
   THINSEG1, THINSEG2) -- no fallback mapped, NOT scored this cycle...`; `gated_segments` contains
   the thin segment and *only* the thin segment; a line-for-line simulation of
   `recompute_gvm`'s loop-skip check against the real returned `gated_segments` set confirms exactly
   the 3 thin-segment symbols are skipped and all 6 control symbols are kept. **PASS.**
2. **Synthetic 2-member segment, WITH a fallback mapped**: logs `... scored against fallback segment
   'Fallback Target Segment' per SEGMENT_FALLBACK_MAP`; the thin segment's peer-average dict is
   confirmed byte-identical to the fallback's own (a `sales_growth_5y` median that differs from the
   thin segment's own pre-substitution value, proving a real swap happened, not a no-op); the
   segment is confirmed **absent** from `gated_segments`. **PASS.**
3. **Clean 20-member single-segment universe**: zero log lines, `gated_segments == set()`,
   `under5 == []` — no false positives on a normal-shaped universe. **PASS.**

`_peer_averages(df).keys()` was also checked equal to the segment set going in, both before and
after `_segment_gate` runs — confirming its return **shape** is untouched, protecting
`gvm_backfill.py`/`gvm_inputs.py`. Script: `test_n5_gate.py` (scratchpad, not committed — a pure
test harness, not a project file).

**Live-integration confirmation** (post-push, once Railway has deployed this commit): a
`gvm_recompute` call against today's already-clean universe (zero real sub-5 segments left, per
item 2 above) is the natural next check — `segment_under_5` and `segment_gated_rows` should both
come back `[]`, proving the guard is a true no-op on clean data end-to-end, not just in isolation.
Posted as a `cc_task_logs` follow-up once confirmed, not blocking this report.

## Item 4 — retire the two empty segment names from any registry/preset that lists them

Grepped `Plastics and Packaging|Textiles Smallcap` repo-wide: **9 files**. 8 are `reports/*.md` /
`reports/*.json` — historical documentation of how these segments got here; left untouched, they
are records of what happened, not live registries. **One real code file:** `qb_smallcap_select.py`
— the Small Cap V2 selection engine's `THEME_SEGMENTS` map (segment -> one of 8 themes, used to
gate which segments even qualify a stock for a Small Cap V2 theme).

Checked both names specifically, boundary-aware (`grep -n "Plastics|Packaging|Textiles"` over the
whole file, not just the two exact strings, to rule out a near-miss): `"Plastics and Packaging"`
never appeared in this file at all (HITECHCORP/KANPRPLA/KNACK were never theme-eligible under that
name — unrelated to today's move, not a regression). `"Textiles Smallcap"` appeared once, inside
`Consumption & Lifestyle`'s segment list — removed. `"Consumer Plastics & Others"` (a genuinely
different, real segment two words later on the same line) is untouched, confirmed by grep after
the edit. `ast.parse` + `py_compile` clean.

**Retirement only — not a re-scope, and here is the gap that leaves open.** The card's item 4 asks
to *retire* the empty names, not to re-map their former members' new segments into the registry.
Doing the latter unasked would be exactly the kind of business judgement item 1 says isn't CC's to
invent silently. So it is flagged, not fixed here:

- `ORBTEXP` moved to `Home Textiles & Technical`, **already** in `THEME_SEGMENTS` under
  `Consumption & Lifestyle` — no coverage change, nothing to do.
- `PASUPTAC`, `NAHARINDUS`, `LAKSHMIMIL` moved to `Synthetic Fibres & Yarn`, which is **not** listed
  under any theme. These 3 real, currently-scoring companies silently lost Small Cap V2 theme
  eligibility as a side effect of a segment-taxonomy cleanup that had nothing to do with Small Cap
  V2's own theme design. Posted as a finding on cc#1199 (below) with a one-line proposed fix
  (add `"Synthetic Fibres & Yarn"` to the `Consumption & Lifestyle` list, the same theme
  `Textiles Smallcap` itself was already in) for Fable/founder to rule on.

## Verify

- `ast.parse` + `py_compile` clean on both `gvm_nightly.py` and `qb_smallcap_select.py`.
- N5 gate: `SELECT segment, COUNT(*) FROM gvm_scores WHERE segment IS NOT NULL GROUP BY segment
  HAVING COUNT(*) < 5` → zero rows, live, just confirmed.
- Guard: 3/3 synthetic cases pass against the real committed `_segment_gate`/`_peer_averages`, log
  lines captured and asserted on directly (not just "no exception").
- `_peer_averages`'s `Dict[str, Dict]` return shape confirmed unchanged (its 3 external callers are
  unaffected).
- `qb_smallcap_select.py`: `"Textiles Smallcap"` confirmed removed (grep, 0 matches outside this
  report's own prose); `"Plastics and Packaging"` confirmed never present; every neighboring
  segment string in the same list confirmed untouched.
- `scheduler.py` / any other caller of `recompute_gvm` untouched — the return dict gained two new
  keys, nothing existing removed or renamed.

Nothing under `worker/**`. `do_not_touch` respected: no scoring-formula change beyond the guard
itself, HEG/GRAPHITE (cc#1985) untouched, the 149 moves (cc#1996) untouched.

Files changed: `gvm_nightly.py`, `qb_smallcap_select.py` (+ this report). Data change: 7-symbol
`input_raw.gvm_segment` move (item 2, listed above). Card **NOT** set done — Fable verifies.
Finding posted separately to cc#1199 per this card's own `finding_rule`.
