# cc#2138 — New Custom Alert modal: metric-type field washed out on light theme

## Where
`scorr_custom_alert_create.js` — the whole modal is one JS-built stylesheet (`CSS` string) and one
overlay `#scorrCacOv`, injected on every page by `main.py _MOBILE_HEAD` and opened from the bell's
"+ CUSTOM ALERT" button (`scorr_bell.js`). It uses the **web token contract** (`--txt`, `--mut`,
`--panel`, `--card2`, `--line2`, `--blu`), confirmed by reading its own CSS; the V8 dashboard, where
the founder opened it, carries `scorr_web_tokens.css`.

## The bug, measured before touching anything
Playwright harness with the real `scorr_custom_alert_create.js`, the real token files and the real
`alert_metric_registry` (10 metrics, 5 categories), picking "Day Move % (%)" on Condition 1 exactly
as in the screenshot, then measuring WCAG contrast of every text element against the first opaque
background behind it:

| Case | Element | Before | After |
|---|---|---|---|
| web light (`html[data-theme=light]`) | Condition 1 metric field, **picked** | **1.13:1** (white #fff on #EEF1F8) | **14.24:1** (#17203A) |
| web light | every other field / label / button | ≥ 4.5 | ≥ 4.5 (unchanged) |
| web dark | picked field, all others | ≥ 4.5 | ≥ 4.5 (unchanged) |
| app light body theme (`aquawhite`) | title, Cancel, picker rows | **1.14:1** (r5 chalk on white) | **17.69:1** (#0E1A20) |
| app light body theme | subtitle, labels, toggles OFF, + add | **2.94:1** | ≥ 7.4 |
| app dark body theme (`goldnight`) | everything | ≥ 4.5 | ≥ 4.5 (unchanged) |

Root causes:
1. **The founder's bug**: `.cac-metricbtn.picked{border-color:var(--blu); color:#fff}` — the picked
   state forced white text while the field kept its light `--card2` background. Readable only by
   accident on dark.
2. **Same class, same modal, app light theme**: the modal reads `--txt`/`--mut`. On an `/m/*` page
   those resolve to `scorr_theme_r5.css`'s chalk (its `:root` rule matches because `<html>` is pinned
   dark), while the body theme (`aquawhite`) turns `--panel` white — light text on a white box. The
   two token systems disagree on the *name* of the text colour: web contract = `--txt` (and `--ink`
   is a page background there, #F5F7FB on light); app body themes = `--ink` (no `--txt` at all).

## The fix (text colour only)
- `.cac-metricbtn.picked`: `color:#fff` removed; the field keeps its own text token and the picked
  state is the blue border + weight 700. Selector, before → after:
  `#scorrCacOv .cac-metricbtn.picked{border-color:var(--blu,#4d7cfe);color:#fff}` →
  `#scorrCacOv .cac-metricbtn.picked{border-color:var(--blu,#4d7cfe);font-weight:700}`.
- Text tokens resolved by **evidence at every `open()`** (`_paintTokens()`): reads `--panel` and
  `--card2` off `body`, and for each surface takes the first of `--txt`/`--ink` (muted: `--mut`/
  `--muted`) whose luminance sits on the opposite side of that surface's — a token that would vanish
  against its surface is skipped. Set as `--cac-txt` (box-backed text: title, subtitle, labels,
  picker rows, Cancel), `--cac-txt2` (field-backed text: inputs, metric buttons) and `--cac-mut` on
  the overlay. The CSS reads `var(--cac-txt, var(--txt,#e9e9ee))` etc., so with no usable token it
  behaves exactly as before. Two surfaces because on an app light theme the box is white and the
  fields are dark (`--card2` from r5) — one text colour cannot serve both. Ratchet: the literal
  fallbacks are the same 10 the file already had, re-wrapped; 0 new.
- Exact tokens in use after the fix: web light `--txt` #17203A / `--mut` #5B6785; web dark
  `--txt` #E9EEFB; `aquawhite` `--ink` #0E1A20 for the box, r5 `--txt` chalk inside the dark fields,
  `--muted` #46585F; `goldnight` `--ink` #F5F2EA.

## Seen, not touched
- The three accent-filled buttons (above/below ON, AND/OR ON, Create) are white bold text on the
  brand blue: 3.73:1 on web dark, 4.35:1 on the app themes. That is the app's standard ON treatment
  (bold text, above the 3:1 large-text bar), not this bug class, and dark theme is do_not_touch —
  left as is, named here.
- Placeholder text colour is browser-default and was not measured.

## Verify
`node --check` clean. Harness: 4 theme cases × 16 elements, before and after; screenshots of the
open modal on web light (before: the picked field's text invisible; after: readable) and on
`aquawhite` looked at (VISUAL_VERIFY_GATE_V1). Alert logic (chaining, search, thresholds, submit)
untouched. Arpit/Fable: open the bell → + CUSTOM ALERT on the V8 dashboard in light theme and pick a
metric — the picked field should now read in dark text inside the blue-bordered box.
