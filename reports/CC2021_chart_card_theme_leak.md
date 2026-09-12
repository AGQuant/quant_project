# cc#2021 — chart card theme leak: `_detectTheme()` only ever matched the literal theme "dark"

Founder screenshot, 12-Sep-2026: `/m/home` in a light theme (aquawhite, the confirmed live light
default), tapped a stock row's **C** button to open the shared chart card
(`window.ScorrChartCard.open`, `scorr_chart_card.js`). The card rendered with the **dark** hardcoded
palette (`_pal()`'s dark branch: navy `#1c2536` panel) sitting directly under the light app header.

## Root cause — confirmed by reading the code, not assumed

`_detectTheme()` checked the raw `data-theme` attribute against the two literal strings `"dark"`
and `"light"` before falling back to a `getComputedStyle(document.body).backgroundColor` luminance
read. The app's real theme system (`scorr_themes.css`, `body[data-theme="X"]` blocks) has **15
named themes**, and exactly one of them is spelled `"dark"`; none is spelled `"light"`. So the
literal check only ever succeeded for the single theme named `dark` — **every other theme, light
or dark alike**, fell through to the `backgroundColor` fallback. That fallback was reading a
property scorr_themes.css never actually sets on `body` (the token layer declares CSS **custom
properties**, `--panel`/`--field`/etc., not `body.style.backgroundColor`), so it was not a reliable
signal — on `aquawhite` it apparently read a dark-enough value to pick the dark palette.

## What this push lands — `scorr_chart_card.js` only, one function plus one small helper

- New `_hexLuminance(hex)`: the exact same luminance formula/threshold the old code used on an
  `rgb()` triple (`R*0.299 + G*0.587 + B*0.114`, `< 110` = dark), applied to a `#hex` value instead
  (3- or 6-digit, leading `#` optional). Returns `null` on anything that doesn't parse, so the
  caller can fall through rather than trust a bad read.
- `_detectTheme()`'s fallback branch (the two literal checks are **untouched**) now reads the
  active theme's **own contract key** — `getComputedStyle(document.body).getPropertyValue("--panel")`,
  falling back to `--field` if `--panel` comes back empty — and classifies light/dark from that hex
  via `_hexLuminance`. **Registry-derived, not a hardcoded theme-name list**: every current theme in
  `scorr_themes.css` already declares both `--panel` and `--field`, so adding or renaming a theme
  needs no update here — the exact property this card asked for over a per-name array.
- `opts.theme` explicit-override callers (`_theme = opts.theme || _detectTheme();`) are unaffected
  by construction — they never call `_detectTheme()` at all when `opts.theme` is set, untouched.
- `_pal()`'s two hardcoded colour objects: untouched, per do-not-touch — this stays a two-bucket
  light/dark approximation, not a per-theme exact-colour rewire (flagged as a possible separate
  follow-up, not folded in here).

## Verify — done here, against the real shipped functions

- `node --check scorr_chart_card.js` — clean.
- The real `_hexLuminance` and `_detectTheme` extracted verbatim from the shipped file (not
  reimplemented) and run in Node against a stubbed `document`/`getComputedStyle`, 24/24:
  - `data-theme="dark"` / `"light"` still hit the literal fast path with `--panel`/`--field` left
    unset — proving the fast path is genuinely never disturbed.
  - **Every one of the 15 live themes in `scorr_themes.css`**, using their real `--panel` hex
    values, classifies into the correct light/dark bucket: `aquawhite`, `goldday`, `blush`,
    `silvergold`, `duskday`, `orangepeel`, `rosewall` → light; `dark`, `goldnight`, `ainight`,
    `winepurple`, `indigoash`, `electricviolet`, `rosenight`, `duskviolet` → dark. This is the
    direct proof that `goldnight`/`ainight` (named by the card as needing fresh confirmation, since
    plain `dark` already "worked" by the old code's own literal-string luck) now correctly resolve
    dark, and that every light theme other than the never-existing literal `"light"` now correctly
    resolves light.
  - `--panel` empty → falls back to `--field`, still classifies correctly.
  - No panel/field at all, or garbage values → falls back to `"light"` (the function's own final
    fallback), never throws.
  - 3-digit hex shorthand and a missing `#` prefix both parse correctly in `_hexLuminance`;
    unparseable input returns `null`.
- `grep` confirms `_theme = opts.theme || _detectTheme();` (line 1692) is unchanged.

## Verify — NOT done here (the card's own FOUNDER-ONLY item)

A live open of the chart card on `aquawhite`/`silvergold`/`duskday` (must now render light) and on
`goldnight`/`ainight` (must still render dark, newly-confirmed rather than accidental) — this
container has no route to scorr.in, stated in the card itself, not worked around.
