# cc#2003/cc#2006 prerequisite — a standalone chain-grid popup API

`window.ScorrCockpitCard.openChain(symbol)`, added to `scorr_cockpit_card.js` (the SAME file
cc#2004's frontend just landed in, sha `74f383d`). This is what cc#2003 means by "the popup
embeds THIS card component" — reuses the exact chain rendering (tag dots, full-row max-pain/wall
highlighting, tap-to-detail panel, (i) explainer, legend) the D-button's own section 06 uses,
in a minimal standalone sheet instead of the full Derivative Cockpit (no volume/OI/futures-basis/
levels sections). cc#2006 (the Market Mood capsule) can call the identical function — "ONE popup
implementation shared by both entry points," per its own spec.

## What this lands

- `chainPopupOpen(sym)` / `window.ScorrCockpitCard.openChain(sym)` — opens the same `#dcOv`/
  `#dcSheet` overlay the D-cockpit uses (reusing its CSS, its close button, its click-outside-to-
  close handler) but fills the sheet with just a title bar + the chain container, then calls the
  existing `dcFetchStrikes(sym)` unchanged.
- A small refactor to support this safely: `_dcChainState(sym)` now carries a `boxId` (defaulting
  to `'dcStrikeChain'`, the D-cockpit's own container id), and `dcFetchStrikes`/
  `_dcRenderChainGrid` read/write through it instead of a hardcoded id. This popup happens to reuse
  the SAME id (one shared overlay, only one of the two "modes" ever showing at once) so the
  refactor isn't load-bearing for *this* call site today, but it removes a latent DOM-id-collision
  risk for any future caller that wants its own independent container, at zero behaviour change for
  the existing D-button entry point.
- `chainPopupOpen` also added to the existing "guarded bare globals" list, matching this file's own
  established convention, in case a host page prefers `onclick="chainPopupOpen('NIFTY')"` over the
  `window.ScorrCockpitCard.openChain(...)` call.

## Verify

- `node --check` clean.
- Playwright, against the real file: `openChain('NIFTY')` opens a sheet containing ONLY the title
  and the chain (confirmed `hasVolumeSection: false` — proves it is not silently the full
  cockpit), the max-pain row is correctly coloured, the popup's own close button closes it.
  **Regression check**: the full D-cockpit (`open('RELIANCE')`) still opens correctly afterward and
  its own `FETCH STRIKES` button still fetches and renders the chain — the `boxId` refactor changed
  nothing for the existing entry point. Zero console/page errors.

## What this does NOT include

The actual Home page wiring (removing the Derivatives section, making the Max Pain chart's chart
area call this new function) — `mobile/home.html` recon is in flight; that file's edits land in
the next push. Neither cc#2003 nor cc#2006 is done — this is the shared API both of them need,
built once rather than each inventing its own popup.
