# cc#2067 — phone status bar icons unreadable on light themes

Founder ask, 14-Sep-2026, screenshot of Daily Digest on a light theme: the phone's own status bar
strip (clock/battery/signal — not app content) is washed out, barely visible. Asked to darken it
enough that the icons stay legible.

## Root cause, re-confirmed before editing (Step 1 gate)

The card's own investigation was right about the mechanism, and re-confirmed by direct read rather
than trusted blind: every `mobile/*.html` page sets `<meta name="theme-color">` to the active
theme's own `--field` token — the OS/browser uses this to paint the status bar background, and (on
most browsers) infers icon colour from its luminance. Nothing was telling the OS the icon colour
**explicitly**, so a light `--field` (Aqua White `#F4F9FB`, Dusk Day `#F4F2FA`, …) left icons that
were only ever tuned to sit on a dark fallback (`#0A0A0C`) hard to read — some browsers/OEM skins
infer the flip from theme-color luminance correctly, others don't, matching "washed out" rather
than a total blackout.

**Two additional things found while re-confirming the card's own claims, not in its root-cause
section:**

1. **The mechanism is NOT purely per-page.** `pwa_endpoints.py`'s `APP_THEME_RESOLVE_JS` (injected
   into every `/m/*` response by `main.py`'s middleware — the same mechanism cc#2052 traced this
   session) exposes `window.SCORR_THEME.set()`, the **live in-session theme switch** path, which
   *also* syncs `theme-color` (`cc#1629`). The 30 pages' own inline boot-script line only covers
   the **fresh page load** case (painting from whatever theme was already stored). Both had to be
   fixed, or switching theme live would still leave icons wrong until the next full reload.
2. **A separate, already-documented layer exists: the PWA manifest's own static `theme_color`**
   (`pwa_endpoints.py`, `GOLDNIGHT_FIELD`, cc#1938) — permanently Gold Night's dark field,
   regardless of the user's active theme, and explicitly noted in that card's own comment as
   **cached at install** for an already-installed WebAPK. Read in full and deliberately **not
   touched**: cc#1938 itself documents "the installed PWA's system bar takes the MANIFEST colour,
   not the page's meta" for that install-time/splash layer, which — being permanently dark — would
   produce a *mismatched dark bar*, not the *washed-out light bar* the founder actually
   photographed. The symptom he reported matches the per-page/live-switch meta layer, not the
   manifest, so fixing the manifest layer was out of scope here; noted for whoever picks this up
   if a future report describes a dark-bar mismatch instead.

## The fix

Added `<meta name="color-scheme" content="dark">` next to the existing `theme-color` meta in all
30 `mobile/*.html` pages (dark, matching the same dark fallback the existing theme-color tag
already assumes before any JS runs), and extended the code that already syncs `theme-color` — in
**both** places it lives — to also set `color-scheme` to `"light"` or `"dark"`:

- The 27 pages whose boot script updates `theme-color` on load (`check.html`/`login.html`/
  `models.html` have no such script at all — confirmed by direct grep, not assumed — so they keep
  the static `dark` value only, unchanged behaviour, matching their existing lack of dynamism).
- `pwa_endpoints.py`'s `APP_THEME_RESOLVE_JS.set()`, for a live in-session switch.

**Classification is computed, not a hardcoded per-theme list** — WCAG relative luminance of the
resolved `--field` hex (the exact formula used throughout this app's own contrast work, cc#2060
included), `>0.5` → `light`, else `dark`. This was a deliberate choice over hand-classifying the 15
(really 14 live + 1 withdrawn) themes: this app adds new themes often (three more landed in the
week before this card), and a hardcoded list is exactly the kind of thing that silently misses the
next one — the same "derive it, don't enumerate it" principle rule 9 already states for the
scheduler. Both edited call sites already had the field's hex in hand for the existing theme-color
sync, so the added computation is a few lines, not a new dependency.

**One real nuance found by computing it, not assumed:** the classification is based on `--field`
specifically, because that is the exact token the status bar already uses — and `--field` is *not*
always what a person would call that theme's light/dark grouping. Rose Wall's cards and panels are
light pink, but its `--field` (the page's outer background, what actually paints the status bar) is
a saturated dark rose `#B8355F`, luminance 0.136 — correctly classified `dark` here. This is not
the same grouping cc#2060 used (which was about `--hi`/`--panel`, the token behind chip fills, a
different question) — the two reports classify Rose Wall differently on purpose, for different
tokens serving different purposes; noted so the two aren't read as contradicting each other.

**Do not touch, respected:** every theme's actual `--field` value (only read, never changed) and
dark-theme behaviour (`theme-color` still resolves to `--field` exactly as before; `color-scheme`
is a new, additive signal, not a replacement for anything dark themes relied on).

## Verify

`ast.parse` clean on `pwa_endpoints.py`. `node --check` clean on all 30 pages' inline scripts plus
`APP_THEME_RESOLVE_JS`, extracted verbatim (31 files). Real headless Chromium, **the actual
boot-script line extracted verbatim from the committed `mobile/home.html`** and **the actual
`APP_THEME_RESOLVE_JS` extracted verbatim from the committed `pwa_endpoints.py`** run against every
real theme's real `--field` value from `scorr_themes.css` — **12/12 checks pass**:

- All 30 files carry the new static meta exactly once, with the original `theme-color` static meta
  untouched (still exactly once each).
- The boot-script extension is present in exactly the 27 files that have a boot script at all —
  `check.html`/`login.html`/`models.html` correctly excluded, confirmed by direct inspection rather
  than assumed from the card's own text.
- The real boot-script snippet, run against all 15 live `--field` values: `theme-color` still
  resolves to the exact field colour (unchanged), and `color-scheme` classifies correctly on every
  theme — `light` for Gold Day / Aqua White / Blush / Silver Gold / Dusk Day / Orange Peel, `dark`
  for the other nine (Rose Wall included, per the nuance above). Zero page errors.
- The real `SCORR_THEME.set()` (live-switch path), run against all 10 themes the switcher actually
  offers: same correct classification, zero page errors.
- Dark themes' `theme-color` value is byte-confirmed unchanged by this card.

**FOUNDER-ONLY, not done here** (this container cannot render or screenshot a phone status bar):
an on-glass screenshot on Dusk Day and one other light theme confirming the icons are now legible,
and a dark-theme screenshot confirming no visible change. Per the card's own explicit sequencing
("TRY THE PROPER FIX FIRST... IF THAT ALONE DOES NOT FIX IT, confirm before assuming... apply the
fallback"), **the darkened-status-bar fallback (scope item 2) is deliberately not implemented in
this push** — it is gated on that screenshot confirming `color-scheme` alone isn't enough on the
founder's actual device/browser. If it turns out to be needed, it's a small, fast follow-up: a
distinct status-bar-only token, not a change to any visible `--field`.
