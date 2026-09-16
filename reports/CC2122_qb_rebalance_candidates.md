# cc#2122 — QB Rebalance History: AWAITING CONFIRMATION shows its candidates (P1 UI BUG)

## Root cause, confirmed against real rows before touching code
The spec's own investigation was unusually precise, and every claim in it was independently
re-verified against the live DB before any code changed — `quant_rebalance_log id=411` (large_cap,
07-Sep-2026) and `id=403` (contra_value, 07-Sep-2026) were read directly, and both match the
spec's citation exactly: 15 candidates (SOLARINDS...BAJAJFINSV) and 4 candidates (HBLENGINE,
SIRCA, CANTABIL, AYE) respectively, `entry_status='awaiting_founder'`, `entry_priority='highest
ret_1y (1-year return)'`, `cap_max_stocks` 15 / 10.

`_rebalance_blocks()` (`qb_endpoints.py`) already computed `cands = a.get("entry_candidates")` and
used it to set `state`/`n_candidates` — but never put the candidate list itself on the returned
block. `/api/qb/gated_rebalances` (the card-face endpoint, untouched by this card) already read
the same field correctly. `rbBlockHtml()` (`quant_basket.html`) had no branch for `AWAITING
CONFIRMATION` at all — it always rendered the DONE block's Sell/Buy tables, which are empty by
definition before a rebalance executes, producing the "No sells / No buys" the founder saw. Both
ends had the number; neither end carried or drew the rows.

**A finding beyond the spec's own investigation**: candidate object SHAPE differs by basket
engine. `large_cap`'s candidates carry `rank`/`dgvm`/`mcap_rank`/`rank_score`; `contra_value`'s
carry `mcap`/`above_20dma` instead — confirmed on the real rows, not assumed. The candidate table
had to be null-safe per field, not built assuming every basket's engine produces the same shape.

## What was built
**Server** (`qb_endpoints.py`, `_rebalance_blocks`): the block dict gains `candidates` (the array
AS STORED — already rank-ordered in the log row, never reshaped or re-sorted here),
`entry_priority`, `cap_max_stocks`, `entry_status`. Pure addition — `n_candidates` and every other
existing key on the block is untouched.

**Client** (`quant_basket.html`, `rbBlockHtml` + new `rbCandTable`): when `b.state === 'AWAITING
CONFIRMATION'`, a candidates table renders INSTEAD of the Sell/Buy tables (never alongside — a
"No sells / No buys" line on a block that has not run is exactly the noise that misled the
founder). Columns: S.No / Stock / Rank / GVM / dGVM / Segment / Slot value, each cell null-safe
(an em dash when the basket's own engine never set that field, never `undefined` or a fabricated
`0`). A selection-rule line above the table states `Top N by <entry_priority> · equal weight, slot
<slot_value>`, sourced from the fields the founder needs to judge the list (why these N, how many
will actually be bought).

**HONESTY_CONSTRAINT, checked field by field on the real rows before writing markup**: a candidate
carries no `price`, no `qty`, no `amount` — `slot_value` is the intended rupee slot, not a traded
amount. The candidate table has its own header row, no Price/Qty/Amount column, and no `Buy`
action pill — grepped the new markup to confirm (`rb-act` — the Buy/Full-Sell pill class — appears
nowhere inside `rbCandTable`'s output). The block carries its own amber visual family (`.rb-chip
.await` already existed; new `.rb-cand-note`/`.rb-rule`/`.rb-tbl tr.rb-cand-row` all key off the
same amber, distinct from the sell/buy tables' red/green) so it can never read as an executed
block at a glance.

**DONE and NO ACTION are unchanged.** Confirmed by reading the diff, not by assuming: the non-
awaiting branch's template string is copied verbatim into a `body` variable, character for
character — the only change is that `${body}` now replaces two inline lines, and the substituted
content is byte-identical either way (verified by manual trace of the surrounding whitespace).

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `qb_endpoints.py`. `node --check` clean on both
inline `<script>` blocks in `quant_basket.html`.

**Diff scope**: `qb_endpoints.py`'s diff is a pure 9-line addition to the block dict — confirms
`/api/qb/gated_rebalances`, the sells/buys derivation, the hard-stop exclusion, and the DONE/NO
ACTION state logic (all `do_not_touch`) are untouched. `quant_basket.html`'s diff is CSS additions
plus the new `rbCandTable` function plus the `body` refactor in `rbBlockHtml` — nothing else in
the file changed.

**Real-code Playwright harness**, `rbBlockHtml`/`rbCandTable`/`rbSideTable`/`esc`/`inr`/`lakh`/
`num2`/`fmtDMY` extracted verbatim, fed real production rows:
- **Large Cap, 07-Sep-2026 (id=411)**: 15 candidate rows render, in the exact order stored —
  SOLARINDS, GRASIM, DIVISLAB, BOSCHLTD, BAJAJ-AUTO, BHEL, LENSKART, MOTHERSON, ADANIPORTS,
  ADANIPOWER, PNB, LODHA, HAL, NESTLEIND, BAJAJFINSV. Matches `n_candidates=15`, matches the
  founder's card-face screenshot. Selection rule line reads "Top 15 by highest ret_1y (1-year
  return) · equal weight, slot ₹33,333.33".
- **Contra Value, 07-Sep-2026 (id=403)**: 4 rows — HBLENGINE, SIRCA, CANTABIL, AYE, on a basket
  holding 1 position, exactly as the founder's screenshot shows. Rank and dGVM correctly render as
  em dash on every row (this engine's candidates never carry those fields) — proves the null-
  safety is real, not just a passing case on large_cap's richer shape.
- **No Price/Qty/Amount column, no Buy pill**: asserted directly against the rendered DOM (table
  headers checked, `rb-act` class searched for in the block's HTML) — zero matches.
- **Large Cap, 07-Aug-2026, real DONE block**: sells CGPOWER/ADANIGREEN/ADANIENT, buys
  DIVISLAB/BAJAJ-AUTO/TVSMOTOR — every price/qty/amount cross-checked against BOTH
  `quant_paper_positions` and the log row's own `entry_detail`/`rank_exit_detail` amounts (all
  agree to the paisa, e.g. CGPOWER 36 × ₹879.00 = ₹31,644, matching the stored `amount` exactly).
  Renders with the ordinary Sell/Buy tables, Price/Qty/Amount columns, coloured Full Sell/Buy
  pills — unchanged.
- **NO ACTION block**: still renders "No sells" / "No buys" correctly (the genuinely-correct case
  this exact text is FOR) and grows no candidate table — proves the fix did not overcorrect into
  hiding the legitimate empty state.
- Zero page errors across every viewport.

**VISUAL_VERIFY_GATE_V1**: screenshotted desktop and 360/375/390px with the AWAITING block and a
DONE block in the same view, as the gate specifically asks (a screenshot of the awaiting block
alone would not show whether it could be mistaken for an executed one). Looked at directly: the
two are unmistakable at a glance — amber "AWAITING CONFIRMATION" chip + amber "PROPOSED — AWAITING
CONFIRMATION, NOT YET BOUGHT" banner + a 7-column candidate table with no money columns, versus
green "DONE" chip + ordinary red/green Sell/Buy tables with Price/Qty/Amount and pills. The NO
ACTION block sits between them showing its correct empty state, and the Contra Value block below
confirms the same pattern holds on a completely different basket with a different candidate shape.
Mobile (375px) shows the same structure; Segment/Slot value scroll off-screen at that width (not
flagged as required to fit by this card's own gate instruction, which asked only for the
awaiting-vs-done distinction to be visible together).

## What did NOT change
`/api/qb/gated_rebalances` and the card-face gate line. The sells/buys derivation from
`quant_paper_positions`, the hard-stop exclusion (HSL tab), the NIFTYBEES cash-residual footer
fold, the DONE/NO ACTION state logic. The rebalance confirm flow (`POST /api/qb/rebalance/confirm`)
— no confirm button was added; this card only displays the pending list. `quant_rebalance_log`
rows and the selection engines — zero writes, read-and-render only.
