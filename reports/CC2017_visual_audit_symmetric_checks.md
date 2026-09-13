# cc#2017 — overflow_clipping / empty_broken: one row per element, not one PASS plus N FAILs

The card's own diagnosis, confirmed by reading `visual_audit.py`'s embedded check JS directly:
`contrast` (35383 FAIL / 110958 PASS) and `tap_target` (18949 FAIL / 11137 PASS) already emit ONE
row — PASS or FAIL — per element evaluated. `overflow_clipping` (31773 FAIL / 161 PASS) and
`empty_broken` (579 FAIL / 161 PASS) did not: each emitted exactly one page-level PASS/FAIL (the
document-scroll check; the error-string check) and then only ever pushed a FAIL, never a matching
PASS, for every individual element that happened to violate a per-element sub-condition. PASS
counts stuck near a flat ~161 (one per capture) while FAIL scaled into the tens of thousands — an
asymmetric-instrumentation artifact, not a real signal that these two checks find defects orders of
magnitude more often than contrast/tap_target do.

## What changed

**`empty_broken`** — the error-string sub-check (page-level, one row) was already symmetric and is
untouched. The per-container sub-check (the `.card, .c, .sect, .oib, .vpage, .oic, .deriv-row,
.apf-note` loop) now pushes `PASS` for a container with visible text, `FAIL` for one with none —
same population, same `text.length === 0` predicate as before, now reported both ways.

**`overflow_clipping`** — the document-level check (one row) is untouched. The per-element loop
now:
- Pushes `PASS` or `FAIL` for the "content clipped, no way to scroll to it" sub-condition on every
  element with text-bearing children (same population and predicate the old FAIL-only code used).
- Pushes `PASS` or `FAIL` for the "extends past the right edge of the viewport" sub-condition on
  every visible element.
- **Excludes** (item 3, the false-positive risk the card itself named) two categories of by-design
  element, neither counted as a defect either way:
  - **By-design horizontal scrollers** — `.chips`, `.rtab`, `.tw`, `.model-nav`, `.dt .tabs`,
    `.vswipe-t`, `#scorrPeerScroll`, `.hscroll` and the like, all real, already-shipped patterns in
    this codebase (confirmed by grep before writing the fix, not assumed). The old code only
    checked the ELEMENT ITSELF for `overflow-x:auto|scroll` — real carousels here are usually an
    outer `overflow-x:auto` shell around an inner flex/track row of chips or tabs, so the wide
    INNER row (not the scrollable outer shell) was the one with `scrollWidth > clientWidth`, and it
    was getting flagged. The new `inScrollableAncestor()` walks the ancestor chain instead of
    checking only `el` itself, so a track/chip/tab row inside a scrollable shell is recognised as
    reachable-by-swipe and excluded from both sub-conditions.
  - **By-design off-canvas content** — a drawer, off-canvas menu, or modal stashed outside the
    viewport on purpose (`position:fixed` plus a transform, typically) is not "clipped" — that word
    means straddling the edge, partly visible and partly cut off with no way to reach the rest.
    `parkedOffCanvas()` distinguishes ENTIRELY off-screen (`r.right<=0 || r.left>=innerWidth`, or a
    `position:fixed` ancestor) from merely straddling, and excludes only the former from the
    right-edge sub-condition. Its own internal-content-clipping sub-condition is deliberately left
    active for such elements — a drawer's own internal text can genuinely be clipped once opened,
    and that is worth catching regardless of the drawer's current on/off-screen transform state, so
    only the sub-condition whose verdict is meaningless for off-viewport content is suppressed.

**Untouched, per the card's own `do_not_touch`:** `contrast` and `tap_target` (byte-identical —
confirmed via `git diff`), the `visual_audit_captures` retention/purge SQL, the
`/api/visual-audit/failures` and capture-viewing endpoints in `visual_audit_endpoints.py`. No new
check name was added — still the same five: contrast, theme_leak, overflow_clipping, tap_target,
empty_broken.

## Verify

`ast.parse` clean on `visual_audit.py`. The embedded `CHECKS_JS` string was extracted and run
through `node --check` on its own — clean (this file has no JS-side test harness of its own;
`ast.parse` alone would not have caught a JS syntax error inside the string literal).

Real headless Chromium, the actual `CHECKS_JS` constant imported directly from `visual_audit.py`
(not re-implemented) and run against a synthetic fixture built to exercise every case the card's
own scope names — **10/10 checks pass**:

- A genuine clipped-content box (fixed width, `overflow:hidden`, oversized inline child, no way to
  scroll to the rest) — still `FAIL`, no regression.
- A well-behaved box whose content fits — now gets an explicit `PASS` row (the core fix: previously
  no row at all).
- A by-design horizontal scroller (`overflow-x:auto` outer shell + wider inner flex track of
  "chips") — its track and chips get **zero** `overflow_clipping` rows, PASS or FAIL: fully excluded,
  not miscounted as a defect.
- An off-canvas drawer (`position:fixed`, `transform:translateX(100%)`, fully parked off the right
  edge) — gets **zero** right-edge/viewport-position rows; its own internal-content sub-check still
  ran and correctly `PASS`ed (not clipped internally).
- A genuine right-edge straddle (absolutely positioned, not inside any scroller, not fixed) — still
  `FAIL`, no regression on the actual defect this check exists to catch.
- `empty_broken`: a card with real text now gets an explicit `PASS`; a truly empty card still `FAIL`s.
- Population sanity: `overflow_clipping` produced 5 PASS / 4 FAIL and `empty_broken` 5 PASS / 1 FAIL
  on this one small fixture page alone — PASS scaling with elements checked rather than staying
  flat, which is the exact shape change the card's own verify list asked for.
- `contrast` (do_not_touch) still ran normally on the same page, confirming the shared `leaves`/
  `vis`/`push` machinery was not disturbed by edits elsewhere in the same JS string.

Not done here, and not needed for this card: re-running the full 03:00 IST production crawl itself
(this container has no route to scorr.in — the same structural limitation cc#2012/cc#2040 already
documented). The next scheduled real crawl, or an on-demand `visual_audit_requests` re-fire the way
cc#2040 used, is how the new PASS/FAIL shape shows up against live pages; the fixture above is a
faithful stand-in built specifically to reproduce each named case, not a substitute for eventually
watching one real crawl's numbers move.
