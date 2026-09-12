# cc#2022 — card-strip pill sizing: `#dcOv`'s own ID beat cc#1973's class-based fix

Founder screenshot, 12-Sep-2026 10:11 IST: D-cockpit sheet for LTM. C, A, R render as noticeably
taller rounded boxes; D (the active letter on that sheet, a `<span>` not a `<button>`) renders the
correct small square.

## Item 2 — measured first, real Chromium, 390px, before touching a line

`scorr_card_strip.js`'s own `.scorr-card-strip.scorr-cs-app .scorr-cs-b` rule already carries
`min-height:0` (cc#1973), added specifically to beat `mobile.css`'s
`button,...,[role=button],...{min-height:44px}` — and it does: that rule has **0 IDs**, so a
3-class selector beats it on pure class-count, regardless of source order. That was never the
whole story. Reproduced the exact DOM shape a D-cockpit sheet renders — `body.mcards`
(`scorr_card_common.js`'s own `addClass()`) wrapping `#dcOv` (`scorr_cockpit_card.js`'s overlay id)
— with the real served `mobile.css`, the real `scorr_card_common.js` CSS (extracted verbatim by
evaluating its own `var CSS = ...` string-concatenation statement in Node, not retyped), and the
real, unmodified `scorr_card_strip.js`, in headless Chromium at 390px:

| element | before fix | |
|---|---|---|
| `<span class="scorr-cs-b scorr-cs-on">` (D, active) | **20 × 20**, computed `min-height: 0px` | correct |
| `<button class="scorr-cs-b">` (C, tappable) | **20 × 44**, computed `min-height: 44px` | the bug, exact match to the screenshot |

**Root cause**: `scorr_card_common.js` (line 1069) carries
`body.mcards #dcOv .dc-chip, body.mcards #dcOv button{min-height:44px}` — no `!important` on
either side, so this is pure specificity. `#dcOv` is an **ID** selector; CSS specificity compares
ID-count before class-count, so **any rule containing one ID always outranks a rule built only
from classes, no matter how many classes it stacks** — cc#1973's fix could never have won here by
adding more classes; it was fighting a battle a class selector cannot structurally win. The
literal-string span (`D`, active) never matched `body.mcards #dcOv button` at all (it isn't a
`<button>`), which is exactly why only the *tappable* letters were affected and the active one
looked fine — matching the screenshot precisely, not a coincidence.

## Item 3 — the fix, `scorr_card_strip.js` only

`min-height:0` → **`min-height:0!important`** on the same declaration, same file, same rule. This
is the one construct that reliably beats an ID-scoped ancestor rule regardless of specificity or
future changes to that rule — the same idiom this codebase already uses one file over
(`scorr_card_common.js`'s own `#scorrChartClose, #scorrAnaX{width:44px!important;height:44px!important}`)
for an identical "component's own box must not be inflated by an ancestor sheet's rule" problem.
Scoped to `min-height` only — the measurement showed `width`/`min-width` were already correct on
both the button and the span; nothing else needed touching. `mobile.css` and `scorr_card_common.js`
are untouched, per do-not-touch — the neutralising declaration lives entirely inside this module's
own block, as the card requires.

## Item 4 — cross-surface consistency, measured, not assumed

Same real files, one page, five contexts, after the fix:

| context | span | button |
|---|---|---|
| plain page (no `body.mcards`/`#dcOv`) | 20×20 | 20×20 |
| `#dcOv` (the reproduced bug's own case) | 20×20 | 20×20 |
| `#dcOv`, **compact** (table-row) variant | 20×20 | 20×20 |
| `#scorrChartOv` | 20×20 | 20×20 |
| `#scorrAnaOv` | 20×20 | 20×20 |

All ten measurements: the identical 20×20 square. `#scorrChartOv`/`#scorrAnaOv` were not actually
broken today — grepped `scorr_card_common.js` for a bare `#scorrChartOv button`/`#scorrAnaOv button`
rule and found none, only narrower ones (`#scorrChartTfs button`, `#scorrChartTabs button`, neither
matching a bare `.scorr-cs-b`) — but `!important` makes the pill un-inflatable there too, and by any
future per-overlay rule, which is what item 4 asked for (consistency now AND going forward).

## Do-not-touch, confirmed

`mobile.css`'s 44px rule, the cc#1973 hit-area `::after` (left/right −3px, top/bottom −12px, 26×44
cap), the cc#1956 20px-square metrics themselves, the dispatch/locked-letter/`cardAvail()` logic:
all untouched — `git diff` is one word (`!important`) plus the explanatory comment.

## Verify

- `node --check scorr_card_strip.js` — clean.
- The two measurement tables above, both from real headless Chromium runs against the real served
  files (not a specificity argument on paper) — before/after and cross-surface, as items 2 and 4
  each require in the task log.

## Not done here (the card's own FOUNDER-ONLY item)

A live open of the D-cockpit sheet on `/m/home` and a strip on `/m/gvm` — this container has no
route to scorr.in, stated in the card itself, not worked around.
