# cc#2112 — remove the fully-dead 3Y depth-probe machinery in scorr_chart_card.js (low)

## Background
Found while working cc#2110 (grepping for other `data-tf` consumers before shipping the
native-`<select>` TF fix): `_apply3Y()` still did `host.querySelector('[data-tf="3Y"]')`, and
`_probe3Y(_sym)` still fired an unconditional `GET /api/candles/{sym}?tf=3Y&probe=1` on every chart
open, with its result unconditionally discarded. Filed as this card rather than bundled into
cc#2110's P0 UI fix.

Reading the file's own comment (line ~63, pre-existing) confirmed this precisely: cc#2103 (the card
that dropped 3Y/3M from `TF_ORDER` after a founder screenshot showed the row overflowing on phone)
**deliberately left this machinery in place**, on the record as "optional cleanup, not required."
This card is that cleanup.

## Confirmed unreachable, not assumed
Traced the full call graph before removing anything:
- `_load(tf)` is only ever called two ways: `tfSel.onchange` (cc#2110's select — its `value` can
  only be one of the 5 real `TF_ORDER` keys) and `_probeFutures(...).then(() => _load(_tf))` (passes
  whatever `_tf` already was, which can only itself have come from one of those same two call
  sites). No path can ever set `tf`/`_tf` to `"3Y"` — the `tf === "3Y" ? "&tf=3Y" : ""` branch in the
  EOD URL builder was provably dead.
- Because that query param can never be sent, the server can never return the
  `{kind:"unavailable"}` shape the next block existed to handle — that whole `if` block was
  provably dead too, not just "unlikely."
- `_apply3Y`'s own `host.querySelector('[data-tf="3Y"]')` can never match anything — no DOM element
  has carried `data-tf="3Y"` since cc#2103 (pill-based before, select-based since cc#2110 — options
  carry `value`, not `data-tf`, and were never built for a "3Y" key regardless).
- Repo-wide grep (`_probe3Y`, `_apply3Y`, `_3yCache`) confirms `scorr_chart_card.js` is the only
  file that ever referenced any of them — no other chart surface (`mobile/v8.html` etc.) shares
  this code independently, so there was nothing else to check for the same pattern.

## Removed
- `_probe3Y()`, `_apply3Y()`, `_3yCache`, `TF_3Y_REASON` (all four, now fully unused together).
- The `tf === "3Y" ? "&tf=3Y" : ""` ternary in `_load()`'s EOD URL builder — simplified to the
  unconditional `TF[tf]` days-only URL.
- The `rows.kind === "unavailable"` response-handling block immediately after it (unreachable once
  `&tf=3Y` can never be sent).
- The `_probe3Y(_sym).then(() => _apply3Y(_sym))` call in the chart-open callback.
- Updated the file's own explanatory comment (was: "left in place... optional cleanup, not
  required") to state plainly that cc#2112 completed that cleanup, keeping the historical why (3Y
  briefly existed under cc#1566, dropped by cc#2103) rather than deleting the context outright.

## Verification
- `node --check scorr_chart_card.js` → clean (a real parser — proves every brace/paren this removal
  touched still balances correctly across the whole file, not just around the edited lines).
- Fallback theme ratchet (`.js` in scope): 1→1, delta 0. Raw ratchet does not gate `.js` files.
- `git diff --stat`: only `scorr_chart_card.js` changed, 8 insertions / 46 deletions — a pure
  subtraction, no line of *reachable* logic altered (confirmed by reading the full diff: every
  removed line was either a direct call to a function this card deletes, or the always-false
  `"3Y"` branch and its dependent dead block).
- Repo-wide grep post-edit: zero remaining references to `_probe3Y`/`_apply3Y`/`_3yCache` anywhere
  (the file's own explanatory comment is the only surviving mention, as prose).
- **No screenshot**: unlike cc#2110/cc#2111, this card has zero visual or behavioural effect by
  construction — every line removed was provably unreachable before this change, so nothing any
  screenshot could show is different. cc#2108's VISUAL_VERIFY_GATE_V1 is a UI-correctness gate;
  this is not a UI change (same reasoning cc#2109 applied to its own pure-middleware fix).

## What did NOT change
`_paintChrome()`, `_buildModal()`, the native `<select>` TF control (cc#2110), the
Pivots/Fib/Channel/GVM overlay logic, `TF`/`TF_ORDER`/`PIV_TFS`/`TF_LABEL`, `_probeFutures`/
`_futCache` (the *futures*-availability probe — a different, still-live mechanism, not touched),
and every other function in the file.
