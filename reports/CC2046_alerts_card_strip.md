# cc#2046 — /m/alerts idea card: the shared C.A.R.D strip, bottom-left, zero card-height growth

Founder follow-up to cc#2045 (same session): the decluttered idea card is missing the C.A.R.D
strip. Wants it bottom-left, explicit constraint — do not grow the card, shift existing spacing up
to make room instead.

## Scope item 1 corrected before implementing it

The card's own scope item 1 said to add `<script src="/scorr_card_strip.js"></script>` to the
page, and its "context_verified_by_claude_this_session" note said the page does not load that
file. **Both are wrong about the live page**, confirmed by tracing `main.py`'s `auth_gate`
middleware line by line rather than trusting the note: `/m/alerts` is `PROTECTED`-only and
deliberately **not** in `_PWA_INJECT_PATHS` (own comment: "/m/ screens carry their own 5-slot
bottom nav... injecting pwa.js's would put two navigations on one screen") — but the
`_MOBILE_HEAD` shared-asset block (which includes `scorr_card_strip.js`) is gated on a completely
different, unrelated test: `if not _present_in_markup(body, b'href="/static/mobile.css"')`. Since
`mobile/alerts.html` genuinely has no `/static/mobile.css` link, that condition is true regardless
of `_PWA_INJECT_PATHS` membership, and `_MOBILE_HEAD` — `scorr_card_strip.js` included — is
injected into the served page. Confirmed twice: by hand-tracing the middleware's exact variables
for `path="/m/alerts"`, and by replaying `main.py`'s own `_present_in_markup`/
`_find_outside_comments` regex logic in a standalone script against the real file bytes (both
agree: injected). `window.ScorrCardRow` is therefore already defined on the live page — adding a
second `<script>` tag would be an inert, harmless-but-pointless duplicate (the file's own
`if (window.ScorrCardStrip) return` guard no-ops a repeat load), so it was not added. This is
stated plainly in the code and here rather than silently followed or silently skipped.

Also checked, since a `defer` tag executes after parse but the page has its own inline `<script>`:
does `cardBody()` (which now calls `window.ScorrCardRow`) ever run before that deferred script
has executed? No — the only call site is inside `load()`'s async `fetch(...).then()` callback,
which cannot resolve before the synchronous parse (and therefore every deferred script) has
finished. No race condition.

## What changed

**`foot()`** now prepends `window.ScorrCardRow(c.symbol)` (cc#789's single-sourced compact strip,
never hand-rolled) to its own returned markup, and wraps the existing text spans in one
`.ia-ft-txt` div rather than leaving them as N separate top-level spans.

**Why the wrapper div matters**: `.ia-ft` was already `display:flex;justify-content:space-between`.
With the strip as a second top-level flex item, `space-between` correctly pushes it to the *left*
edge and the *last* item to the right — but the footer text used to be several separate `<span>`
siblings, and `space-between` would have fanned all of them out across the row individually instead
of keeping them together as one line. Grouping them into one `.ia-ft-txt` flex item first makes it
exactly two items — strip, text-group — which is what turns `space-between` into "strip bottom-left,
text on the right" instead of a scattered row. Where a card's footer has no text at all (a live FUT
card, since cc#2045), `.ia-ft-txt` is omitted entirely rather than rendered empty, leaving the strip
as `.ia-ft`'s only child, still flush left.

**The padding correction, measured not guessed.** A first pass shifted `padding-top` down by a
flat 6px (24→18 / 10→4) as a starting estimate, then real Chromium measurement (isolating `.ia-ft`
alone, swapping the real strip in and out) showed that was short: this footer's own text content
renders at **~11px** tall, and the strip's own locked pill height (cc#1956 — width/height fixed at
20px, never shrunk here) is a fixed **20px** — a 9px gap, not 6px. `padding-top` was retuned to
**15px / 1px** (from 24px / 10px), which the real measurement below confirms is now exact.

## The one honest exception — not a bug, a structural limit

Three of the four named scenarios (closed, live equity, manual trigger) all had real footer *text*
before this card — trimming 9px off their padding exactly offsets the strip's larger height, and
their measured card height is now **byte-identical** to before. The fourth — a **live futures
card** — is the exact one cc#2045 emptied out to *zero* content in `.ia-ft`. A fixed 20px pill row
cannot fit inside a row that previously needed no height at all; no padding value can make
`20px + padding ≤ 10px_old_total` while padding stays ≥ 0. That card measures a real **+11px**
taller, unavoidably, without shrinking the strip below its own founder-locked size (explicitly
out of scope per `do_not_touch`). Stated here rather than glossed over, per the card's own
"any non-zero delta must be explained or fixed" — this one is explained, not fixed, because a fix
would mean re-litigating cc#1956.

**Untouched, per `do_not_touch`:** the sparkline, the plan tiles and `track()`, the header stats
strip, filter chips, ribbon, symbol/tags row, price block, all `/api/alerts/*` endpoints, and
`scorr_card_strip.js` itself (no fork, no hand-rolled markup — `window.ScorrCardRow` is called
exactly as every other surface calls it).

## Verify

`node --check` clean on the extracted script block. Real headless Chromium, the actual
`mobile/alerts.html` (both the pre-cc#2046 and this card's post-edit version) and the actual
`scorr_card_strip.js`, served at the **literal path `/m/alerts`** — required, since the strip's
own square-pill sizing (cc#1941/1956) keys on `location.pathname.indexOf('/m/')===0`; serving the
file from any other URL silently falls back to the 32px web-sized pill and gives a meaningless
height delta. **21/21 checks pass**:

- Card height, before vs after, per scenario (realistic fixtures — engine ideas carry
  `target`/`stop`/`track`, matching the founder's own screenshot note that a closed card still
  shows its track):
  - closed: 204.0px → 204.0px (**Δ 0**)
  - live futures: 193.0px → 204.0px (**Δ +11**, the explained exception above)
  - live equity: 204.0px → 204.0px (**Δ 0**)
  - manual trigger: 171.0px → 171.0px (**Δ 0**)
- Every scenario renders exactly one strip, never duplicated or hand-rolled, all four letters
  (C/A/R/D) present, and the app-scoped `scorr-cs-app` square-pill class present (confirming the
  `/m/` detection genuinely fired in this test, not just assumed).
- Zero console/page errors and zero unexpected network requests once the harness's own unrelated
  dependencies were stubbed (`/api/alerts/ideas`'s real background fetch, `/api/v8/futures/list`,
  `/static/*`, Google Fonts — the same pre-confirmed sandbox restrictions seen elsewhere this
  session).

Not done here, and not needed for this card: the founder's own live check on scorr.in (tapping
C/A/R/D to confirm the shared overlays open) — this container has no route to the deployed site,
the same structural limitation on every UI card this session. The measured, byte-real height deltas
above are the closest verifiable substitute for "not visibly taller."
