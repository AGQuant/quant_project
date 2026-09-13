# cc#2045 — /m/alerts idea card: strip approved-line, sparkline caption, held/expiry/R:R footer

Founder decluttering ask, 12-Sep-2026 (confirmed item by item across several follow-up messages):
remove the approved-date/price/via-app line from the card header, remove the sparkline caption
below the chart, and remove the held / expiry / R:R-at-entry line from the card footer. Three small
deletions in `mobile/alerts.html`, no backend, no schema — difficulty 2/10 per the card's own rating.

Step 1 (the card's own read-only gate) was done first — exact line numbers and current text posted
to `cc_task_logs` before any edit, per the card's explicit instruction.

## What changed

**Item 1 — `cardHead()`.** Deleted the `appr` line build and its `.ia-appr` render target entirely.
The header now goes straight from the tags row to the LIVE/CLOSED ribbon + price block.

**Item 2 — `spark()`.** Deleted the trailing `.ia-sparkn` caption div. The sparkline SVG itself
(polyline, dot, baseline) is byte-for-byte untouched — only the caption text under it is gone.

**Item 3 — `foot()`, the one part needing real care rather than a pure deletion.** The original was
one 4-branch `if/else if/else if/else` chain (closed → FUT+expiry → OPT → cash equity) followed by a
*separate* 2-branch `if/else if` (R:R at entry → Price-level fallback). A naive delete-the-line
approach breaks both:
- Simply removing the FUT branch would let a live FUT card fall through to the final `else` and
  wrongly show "cash equity" — the card's own spec explicitly says a live FUT card should render
  **nothing** from this branch. Kept an explicit no-op `else if (c.instrument === 'FUT') {}` so the
  chain still recognises FUT and still doesn't reach the `cash equity` else.
- The `else if (c.style === 'Price level')` fallback was chained under the `rr_at_entry` check being
  removed — deleting that check outright would have deleted the fallback with it, even though
  `do_not_touch` explicitly keeps it. Promoted it to a standalone `if`, so a manual trigger card
  still gets `handpicked · founder`.

Net effect, matching the card's own walkthrough exactly: closed → still the close line; live FUT →
empty footer; live EQ → still "cash equity"; OPT → still "option leg"; manual (Price level) trigger →
still "cash equity · handpicked · founder", minus the held prefix.

**Untouched, per `do_not_touch`:** the sparkline SVG itself, the TARGET/STOP/TO TARGET plan tiles and
`track()`, the header stats strip, tab/category filter chips, the LIVE/CLOSED ribbon, the
symbol/direction/tags row, the price/change block, all four `/api/alerts/*` endpoints, the cc#1507
create-alert flow and cc#1634 bell deep-link. The `.ia-appr` and `.ia-sparkn` CSS rules are left in
the stylesheet, unused but harmless — the card's own call, either way was acceptable.

## Verify

`node --check` clean on the extracted script block. Real headless Chromium, the actual
`mobile/alerts.html` loaded unmodified, calling the real shipped `cardHead()` / `spark()` / `foot()`
directly — **17/17 checks pass**:

- `cardHead()`'s output no longer contains `ia-appr` or the word "approved" anywhere; symbol, tags
  and the LIVE ribbon are confirmed still rendering.
- `spark()`'s output no longer contains `ia-sparkn` or "since approval"; the polyline/dot/baseline
  SVG elements are confirmed still present.
- All four `foot()` scenarios traced AND executed against the real function: **closed** → close line
  kept; **live FUT** → completely empty (`<div class="ia-ft"></div>`, zero `<span>`s) — the exact
  net effect the card itself specifies; **live EQ** → "cash equity" kept, no held/R:R; **manual
  (Price level) trigger** → "cash equity" + "handpicked · founder" kept, held prefix gone; **OPT**
  (do_not_touch) → "option leg" kept.
- Zero console/page errors and zero unexpected network 404s once the harness's own unrelated
  dependencies were stubbed (the `/static/*` URL namespace, `/api/alerts/ideas`, and Google Fonts —
  the same pre-confirmed sandbox egress restriction seen elsewhere this session).

Not done here, and not needed for this card: the founder's own live check on scorr.in (this
container has no route to the deployed site). The four-scenario trace above — run against the real,
unmodified function, not reasoned about on paper — is the closest verifiable substitute; the card's
own live-check line is still the final word.
