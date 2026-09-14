# cc#2055 — SCORR_SHARED_CARD_THEME_LOCK_V1: closing the mechanism, not another call site

Founder ask, verbatim: "card button data should be consistent across the app and website" — one
canonical spec for the C·A·R·D shared cards' theming, not fix-it-where-you-find-it. Trigger: two
independent bugs with the identical visible symptom (dark chrome on a light theme), found one at a
time in the same session — cc#2047 (`mobile/gvm.html`'s own local `theme:'dark'` override) and
cc#2052 (a precedence bug inside `_detectTheme()` itself, no override involved at all).

## Step 1 — the full audit, not an assumption

All four C·A·R·D letters, every real caller, app and web, read directly rather than grepped for a
literal string (cc#2047's own limitation — an override could be a variable, not a literal):

| Letter | Component | Theme mechanism | Caller-overridable? |
|---|---|---|---|
| **C** | `scorr_chart_card.js` | JS-computed `_pal()`, driven by `_theme` | **YES** — `opts.theme \|\| _detectTheme()`, confirmed live callers: `scorr_card_strip.js` (`_fallback`), `v8_dashboard.html` (×2), `v10_dashboard.html`, `scorr_result_corner.html`, `scorr_holdings.html` (×2), `mobile/gvm.html`. **Zero** of these currently pass a `theme` key (cc#2047 removed the one that did) — confirmed by a repo-wide grep re-run as part of this card's own verify, not assumed. |
| **A** | `scorr_analysis_card.js` | CSS custom properties inherited from the host page (`var(--panel,#fff)`, `var(--txt,#1c2536)`, `var(--line,...)`) | **NO** — `qaAnalysis(sym)` takes a single argument; there is no second parameter for an override to occupy. Confirmed architecturally immune, not just unused-in-practice. |
| **R** | `pwa_endpoints.py`'s `RESULTS_CARD_JS` (the `ScorrRCard` IIFE — **not** a standalone `results_card.js` file; see the correction below) | Same CSS-inheritance pattern as A (`var(--card,#fff)`, `var(--txt,#101828)`, `var(--line,...)`, `var(--mut,...)`) | **NO** — `open(sym)` is single-argument, same as A. Architecturally immune. |
| **D** | `scorr_cockpit_card.js` | Its own fixed **dark-terminal** palette, deliberately independent of the host theme (cc#571 PART B, "dark institutional terminal" — own comment: "the cockpit's own dark palette never leaks into (or inherits from) the host page theme") | **NO**, and **for a different reason than A/R** — this component was never meant to follow the host theme at all. `open(symbol, side, qty, entry, cmp)`'s five parameters are all trade figures; there is no theme slot to lock, and there should never be one — a future change that made D start following the host theme would be *reversing* an intentional design choice, not closing a gap. |

**Only C ever had the mechanism.** A, R and D are immune by construction (two of them via CSS
inheritance, one via deliberate design), not by luck — stated explicitly in each file so a future
reader does not go looking for a lock that was never needed there.

**Incidental finding, corrected while auditing:** `scorr_card_strip.js`'s own `_LOCKED` map (and
its header comment) named R's implementation as `results_card.js` — a file that does not exist.
That string is purely diagnostic (used only inside `cardNav`'s console.warn when a page tries to
register a now-locked letter), so it was never a functional bug, but it is exactly why this card's
own spec guessed a nonexistent filename for R. Corrected to point at where R actually lives.

## The lock

`scorr_chart_card.js`'s `open()`: `opts.theme` is no longer read for the palette at all —
`_theme = _detectTheme();`, unconditionally. Passing `opts.theme` now does nothing except emit a
diagnostic `console.warn` naming `SCORR_SHARED_CARD_THEME_LOCK_V1`, so a future accidental
regression is caught immediately rather than silently reproducing cc#2047's bug a third time. A
**forward rule**, in the same binding style `scorr_card_strip.js` already uses for its own C/A/R/D
letter lock (cc#789/803/805), is now in the file's own header: no caller may override the
live-detected theme; a future need for a different look extends `_detectTheme()`/`_pal()`
themselves, never adds a per-caller parameter. The header's own API docstring (which previously
*documented* `{theme:'light'|'dark'}` as a real, callable parameter) is corrected to match.

**Untouched, per `do_not_touch`:** `_detectTheme()`/`_pal()` themselves (cc#2047's and cc#2052's
own testing already confirmed these correct; this card removes the override *around* them, never
re-touches the detection logic), the C·A·R·D strip's own letter-to-component dispatch.

## Scope item 5 — reconciling with cc#2052

**They do not subsume each other; both fixes are required, independently.** cc#2052's bug was
never an explicit override — `_detectTheme()` itself was answering the wrong question (reading
`documentElement`'s deliberately-always-dark `/m/*` pin before `body`'s real theme). Closing the
override mechanism here would not have touched that bug at all, and cc#2052's own fix (swapping
the check order) would not have prevented a *future* caller from reintroducing cc#2047's bug by
simply passing `opts.theme` again. Confirmed empirically, not just argued: this card's own
regression suite re-ran cc#2052's full real-injection test matrix against the now-locked file and
it still passes 43/43 — the two fixes are consistent and additive, neither re-opens the other.

## Canonical rule, stated as asked

**SCORR_SHARED_CARD_THEME_LOCK_V1**: *No caller of a C·A·R·D shared card, on any surface — app or
web — may override its live-detected theme. `scorr_chart_card.js` is the only one of the four that
ever carried an override parameter; it is now removed, diagnostic-warned if attempted, and the
other three are confirmed architecturally immune (CSS inheritance for A/R, deliberate host-theme
independence for D). A future look change extends the shared file's own detection/palette, never
adds a per-caller switch.*

## Verify

`node --check` clean on all four `.js` files touched; `ast.parse` clean on `pwa_endpoints.py`.
Real headless Chromium, the real (now-locked) `scorr_chart_card.js` — **13/13 checks pass**:

- `opts.theme:'dark'` passed on a genuinely light page (`aquawhite`) is ignored — chart still
  paints light; the reverse (`opts.theme:'light'` on a dark page) is equally ignored — chart still
  paints dark. Both fire the new diagnostic warning naming the lock.
- Normal auto-detection (no `opts.theme` at all, including GVM's own real `{gvm:true}` call shape)
  is completely unaffected — zero warnings, correct paint on both a light and a dark theme.
- A repo-wide grep re-confirms zero live callers currently pass a `theme` key anywhere.
- cc#2052's own 42-check real-injection regression suite re-run against this same file: still
  **all pass** — the two fixes are consistent, neither reopens the other.

One unrelated, pre-existing gap was found and deliberately **not** fixed here (out of scope for a
theme-lock card): `_ensureLib()`'s script-tag-reuse path only re-listens for `'load'` on a second
`.open()` call, never `'error'` — if the LightweightCharts CDN fails once, a *second* open() in the
same page session never gets a repaint callback. This does not manifest in real production use
(the CDN normally succeeds on the very first load, taking the `window.LightweightCharts` fast
path thereafter) and is a library-loading robustness question, not a theme question — flagged here
for the record rather than silently worked around, with the actual test restructured (one fresh
page per scenario) to avoid tripping it.

Not done here, and not needed for this card: the founder's own live spot-check on scorr.in across
GVM/Home/alerts/V8 (this container has no route to the deployed site) — the card's own verify
section marks that step FOUNDER-ONLY.
