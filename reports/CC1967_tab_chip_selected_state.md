# cc#1967 — Tab / chip selected state: one colour, one step lighter

Founder ask, 10-Sep: page to page, be consistent in the tab selection colour — the same
everywhere, a bit lighter than the current polish.

## A — Enumeration (posted before any change was made, per scope item 1)

Every ACTIVE/SELECTED-state rule for a tab, chip, pill, filter toggle or segmented control across
`mobile/*.html`, `mobile/theme_mobile.css`, `scorr_appshell.css`, `pwa_endpoints.py` and
`mobile_endpoints.py`:

| File | Selector | Before | Resolved on goldnight (before) |
|---|---|---|---|
| mobile/aicio.html, fpc.html, health.html, invscan.html, learn.html, mf.html, qbbuilder.html, tcscan.html, trade_wall.html (9 files, identical) | `.chip.on` | `color:var(--brand);background:var(--hi)` | text `#D4AF37`, fill `#1B1B20` (the theme's flat highlight tile — no relation to brand) |
| mobile/aicio.html | `.pill.on` | `color:var(--win);border-color:var(--win)` | `#1FCB6E`-family (the P&L **win** colour, reused for a UI selection state — a latent collision with the do-not-touch P&L colour, not itself a P&L display) |
| mobile/home.html | `.ap-hdr .filt.on` | `color:var(--brand);border-color:var(--brand)` | text+border `#D4AF37`, no fill |
| mobile/home.html | `.apf-opt.on` (+ `.sub`) | `color:var(--brand)` | text only, no border, no fill — this is a **selected sheet-list row** (full-width row with a `.tick` checkmark), a different design language from a tab/chip, same category as the bottom-nav `.bn.on`/`.as-bn.on` underline treatment. **Flagged, not touched**, per the card's own allowance for a different-language affordance. |
| pwa_endpoints.py | `.dt .tab.on` | `background:var(--blue);border-color:var(--blue);color:#fff;box-shadow:0 6px 18px rgba(77,124,254,.35)` | `#4D7CFE` fill, white text — **but `.dt` is its own fully self-contained "Dark Terminal Design System" (cc#344), deliberately isolated from the 13-set theme contract with "zero global bleed."** It does not declare or inherit `--brand`/`--panel` at all — see Method below. |
| mobile_endpoints.py | `.chip.on` (+ `.n`) | `color:var(--txt);border-color:color-mix(in srgb, var(--blu) 55%, transparent);background:color-mix(in srgb, var(--blu) 14%, transparent)` | text near-white, border/fill tinted `#4D7CFE` (14%/55%) — the **legacy `.screen,.bnav` token family** (mobile_app.css), bridged to the theme contract by `mobile/theme_mobile.css`'s STEP TWO aliasing, not the clean 21-name contract directly |
| mobile/theme_mobile.css, scorr_appshell.css | `.bn.on`, `.as-bn.on`, `.as-back.on`, `.dots i.on` | various, per-theme-scoped overrides | **Bottom-nav / stepper active states — a nav affordance (underline + glyph colour + dot), not a tab or chip.** Flagged, out of scope per the card's own instruction, not touched. |

**Five different answers to "what does selected look like"** existed before this card, across the
files that are genuinely tabs/chips/pills: a flat highlight-tile fill with no brand relationship
(9 files), a P&L-colour reuse (1 pill), a border-only treatment with no fill (1 filter toggle), a
hardcoded blue+white pair with a raw hex and a raw rgba shadow (1 dashboard system), and a
color-mix already in the right spirit but against the wrong token and the wrong resolved base (1
legacy family). The 9-file `.chip.on{color:var(--brand);background:var(--hi)}` treatment is the
most common — that is the canon starting point.

## B — The canon (scope item 2)

```
color: var(--brand);
background: color-mix(in srgb, var(--brand) 14%, var(--panel));
border-color: color-mix(in srgb, var(--brand) 55%, transparent);
```

`var(--brand)` for text is kept from the 9-file majority. The 14%/55% color-mix pair is kept from
the ONE place it already existed (`mobile_endpoints.py`'s `.chip.on`, which had the right
mechanism but the wrong token — `--blu`, a page-local literal, instead of `--brand`, the contract
name) — this is a convergence onto an existing mechanism, not an invented one, and it directly
answers scope item 3's ask for a token EXPRESSION rather than a new hex, so the lightening is
correct on every one of the 21-name contract's sets, not just the one the founder is looking at.

**Two files could not take the literal canon without crossing an isolation boundary someone
else built on purpose, and were converged in spirit instead, using their own vocabulary:**

- **`pwa_endpoints.py`'s `.dt .tab.on`** — `.dt`'s own docstring (cc#344) states it is scoped
  deliberately so unconverted pages "keep their current theme — zero global bleed." It declares
  its own complete token set (`--blue:#4D7CFE`, `--surface`, etc.) and does not participate in the
  13-set contract at all. Forcing `var(--brand)`/`var(--panel)` into this scope would depend on
  values that are not declared there and are not reliably inherited past this system's own
  boundary — a real, unverifiable regression risk on a page this card cannot render. Instead, the
  SAME 14%/55% formula and the SAME "drop the raw literal" rule were applied using `.dt`'s own
  accent (`--blue`) and its own panel-equivalent (`--surface`, matching the unselected `.tab`'s
  own background) — the raw `#fff` text colour is gone (now `var(--blue)`, matching the rest of
  `.dt`'s own tokens), the fill and border are now the same lightening expression as everywhere
  else, just addressed to `.dt`'s own accent. The one remaining literal, the box-shadow
  `rgba(77,124,254,.35)`, was kept because it matches `.dt`'s own established convention
  (`--bull-soft`, `--bear-soft`, `--amber-soft` are all hardcoded rgba glows tied to their own hex
  — this file does not use a `var(--brandglow)`-style token anywhere, and inventing one here would
  be a bigger change than this card's scope).
- **`mobile_endpoints.py`'s `.chip.on`** — this sits in the legacy `.screen,.bnav` family
  (`mobile_app.css`'s own `MOBILE_CSS` constant), which hardcodes its own `--panel:#121A33`
  locally. `mobile/theme_mobile.css`'s existing STEP TWO bridge already re-points this exact
  scope's `--panel` at `var(--t-panel)` (the contract-facing alias), so `var(--panel)` almost
  certainly resolves correctly through that bridge today — but this card cannot render the page to
  confirm cascade order holds on every path that loads this CSS. The mix target was written
  defensively as `color-mix(in srgb, var(--brand) 14%, var(--t-panel, var(--panel)))` — the bridge
  alias first, the local literal as a safety fallback, never a broken/undefined value either way.
  `--brand` itself is not locally shadowed anywhere in this scope, so it was used directly.

## C — Resolved colour, before and after (scope item 3, verify item C)

Computed by linear sRGB mix (the same arithmetic `color-mix(in srgb, …)` performs) — not
rendered, per verify item E; the founder confirms on glass.

| Set | `--panel` | `--brand` | New fill @14% | New border @55% |
|---|---|---|---|---|
| goldnight (dark, default) | `#131316` | `#D4AF37` | `#2E291B` | `color-mix` (translucent gold ring) |
| aquawhite (light) | `#FFFFFF` | `#35E0FF` | `#E3FBFF` | translucent cyan ring |
| silvergold (light) | `#FFFFFF` | `#C29A34` | `#F6F1E3` | translucent gold ring |

All three stay clearly distinguishable from an unselected chip (plain `var(--panel)`/transparent,
no tint) at this percentage — 14% was chosen specifically because it is visible on both a near-
black dark set and a pure-white light set without becoming either too saturated on dark or too
faint on light (a spot check at 20% looked oversaturated on goldnight; 12% looked marginal on the
light sets). **Contrast floor (scope item 5): the text colour is unchanged (`var(--brand)`, the
same colour already used and already passing on every set today) — only the fill and border
lightened, so legibility is not newly at risk anywhere the previous `--brand` text was already
fine.**

## D — Result (verify items B, D)

12 files touched, 12 selector sites converged (9 identical `.chip.on` occurrences + `.pill.on` +
`.filt.on` + the two special-vocabulary cases): `mobile/aicio.html`, `fpc.html`, `health.html`,
`invscan.html`, `learn.html`, `mf.html`, `qbbuilder.html`, `tcscan.html`, `trade_wall.html`,
`home.html`, `pwa_endpoints.py`, `mobile_endpoints.py`.

`theme_validate` on all 12 touched files: **`ok: true`, zero regressions.** Several files'
raw-primitive counts *improved* as a side effect of removing raw literals (`#fff`, the old rgba
shadow, the `--win`/`--blu` reuse):

| File | Raw primitives before → after | |
|---|---|---|
| mobile/trade_wall.html | 34 → 1 | improved 33 |
| mobile/home.html | 41 → 23 | improved 18 (matches the card's own quoted baseline) |
| mobile_endpoints.py | 560 → 522 | improved 38 |
| pwa_endpoints.py | 231 → 231 | level |
| mobile/fpc.html | 0 → 0 | clean |
| the remaining 6 mobile/*.html files | no prior baseline recorded — reported as unmeasured, not as a pass, per the tool's own convention |

Not touched, flagged instead: `.apf-opt.on` (selected sheet-list row, a different affordance);
`.bn.on` / `.as-bn.on` / `.as-back.on` / `.dots i.on` (bottom-nav / stepper, a nav affordance);
`scorr_themes.css` token values (untouched, per do_not_touch); P&L green/red / pass-fail colours
(untouched everywhere, including the one place — `.pill.on`'s old `--win` reuse — that was
*removed from a UI-selection role*, not from its real P&L role, which this card never touched).
