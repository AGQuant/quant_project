# cc#2066 — Index Trades tile in Home ANALYTICS, a permanent entry point

Founder ask: the NIFTY/BANKNIFTY option-writing positions view (Hero Card 4, "Index Positions")
only appears when a position is open. Wants it reachable in one click regardless.

## Step 1 gate — confirming the real mechanism before building

Traced the actual call chain, not assumed from the card's own description: Card 4's `VIEW LOGS`
button calls `openV10(event,'NIFTY50','OPT',true)` — toggles the static `#v10ov` overlay (already
in the page's own HTML template, not built by `renderHome()`) and calls `loadV10()`, which fetches
`/api/mobile/v10chart`. `syncHeroPositionsCardVisibility()` is a **completely separate** system
that adds/removes Card 4 from the hero swipe deck based on `niftyFlat && bnfFlat` — it has no
connection to `G_GROUPS`/the ANALYTICS grid at all, so a new grid tile is unconditionally present
by construction; there was never a risk of coupling the two.

**The mechanism decision, per the card's own item 2**: built approach (b), the hash deep-link —
confirmed it's the only one that actually delivers "one click from anywhere in the app." `gtileR1()`
(every `G_GROUPS` tile's renderer) is a plain `<a href>`, with no support for an `onclick` handler
at all — checked directly, not assumed — so a same-page JS trigger needed a URL-based approach
regardless. `mobile/home.html` had no prior hash-handling of its own to extend (the `#new`/`#ia-<id>`
precedents the card cited live in other files); this is the first one in this file, following the
same shape.

**Placement, decided from evidence found while building**: the card defaulted to "the 12th tile,
completing the row," but `mobile/home.html`'s own cc#1925 comment pins QB Builder as explicitly
**"LAST in Analytics"** — a founder-set position this card doesn't touch. Placed the new tile
directly **before** QB Builder instead, satisfying both: it's still near the group's end (close to
the card's own suggestion) and QB Builder's own prior "last" designation stays true, not silently
overridden.

**Icon**: `heroCardPositions()` has no icon of its own to reuse — checked, not assumed. Built a new
one (two simple bars, one per index) in the same plain-geometry `.f`-fill-plus-stroke family GVM
and Trade Check already use, rather than inventing a different visual style for one tile.

## The fix

- New tile: `{ href: '/m/home#index-trades', label: 'Index Trades', web: false, svg: ... }` in
  `G_GROUPS`'s Analytics group, directly before QB Builder.
- Home's own boot sequence (the tail `Promise.all([...]).then(...)` that calls `renderHome()`) now
  checks `location.hash === '#index-trades'` after the page's own initial load finishes, and calls
  `openV10(null, 'NIFTY50', 'OPT', true)` — the **exact same call** the hero card's own button
  makes, so whatever empty-state handling `openV10`/`loadV10` already have for a flat book (the
  card's own citation: "openV10/loadV10 already do this for other callers") applies identically
  here, with nothing new to build for that case.

**Do not touch, respected**: `syncHeroPositionsCardVisibility()` — confirmed present, unmodified.
The `VIEW LOGS` button's own `onclick` — confirmed byte-identical. Every other grid tile —
confirmed unchanged.

## Verify

`node --check` clean. Real headless Chromium, the actual `openV10()` function and the hash-check
line **extracted verbatim from the committed file**, run against a real static `#v10ov`/`#v10bd`
DOM — **18/18 checks pass**:

- The new tile exists with the right href/label, sits directly before QB Builder inside the
  Analytics group specifically (isolated from `G_GROUPS`'s other two groups, not just checked
  against the whole array's own end), and QB Builder is confirmed still the Analytics group's own
  last tile.
- **With the hash present**: the overlay actually opens (`#v10ov` gains `.open`), `V10.sym`/`leg`/
  `legLock` are set to the exact same values the hero card's own button sets, body scroll locks,
  and `loadV10()` is actually invoked once — the real mechanism fires, not just markup that looks
  right.
- **With no hash**: the overlay stays closed and `loadV10()` is never called — this tile changes
  nothing about every other visit to Home.
- **A near-miss hash** (`#index-trades-typo`) does not accidentally trigger it — confirms an exact
  match, not a loose prefix check.
- `do_not_touch` items (`syncHeroPositionsCardVisibility`, the hero card's own button) confirmed
  byte-unchanged.
- Zero page errors across every case.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that the tile opens the same content the hero card shows, and that it works even when
Card 4 is currently off the deck.
