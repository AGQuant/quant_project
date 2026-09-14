# cc#2079 (low) — Custom Screen Builder: picker button font size, +2 notches

Founder ask, verbatim: increase the font size of all four picker button rows in the Custom Screen
Builder card — Buildup, Orderflow, Basis band, Volume R band — by 2 notches.

## Where

`v8_dashboard.html`, Index Intel pane, Custom Screen Builder card (`rCustomScreen()`, cc#1822/
cc#1881). All four rows (`csbRow('Buildup', ...)`, `'Orderflow'`, `'Basis band'`,
`'Volume R band (first guess)'`) render their buttons through the **one** shared `csbChip()`
function and its **one** CSS class, `#pane-index .csbchip` — so a single rule change covers every
row uniformly, exactly matching the card's own scope_note ("same size increase applied uniformly
across all of them").

## "Notch" — the card's own stated fallback, applied

Checked, per the card's own instruction, whether this page has a discrete stepped type-scale to
move up instead of guessing a pixel value: `scorr_web_tokens.css` and `scorr_theme_r5.css` were
both read — `--f-d`/`--f-m` are font-**family** variables (display/mono), not a size scale, and
neither file defines any `--fs-*`/stepped-size tokens at all. No such scale applies to these
buttons, so this uses the card's own stated working fallback: **+2px per notch, +4px total**.

**Before**: `font:700 10.5px var(--f-m)` → **After**: `font:700 14.5px var(--f-m)`. Weight and
font-family untouched; padding (`5px 12px`) untouched — confirmed unnecessary to change, see Verify.

## Verify

Real headless Chromium render of the actual extracted `.csbchip`/`.csbrow`/`.csblb` CSS rules
(verbatim from the file) with real button markup (`csbChip()`'s own output shape) for all four
rows, 16 buttons total:

- Computed `font-size` on a real `.csbchip` element is genuinely `14.5px` — the rule applies, not
  just present in the source.
- **Zero text clipping** on any of the 16 buttons at the new size, with padding left exactly as it
  was — checked via `scrollHeight`/`scrollWidth` vs `clientHeight`/`clientWidth` on every button,
  not eyeballed. This is the real evidence behind "do not change padding unless the larger text no
  longer fits" — it fits, so padding was left alone.
- The four rows do not visually overlap each other at the taller button height (checked via real
  `getBoundingClientRect()` on each row).

**Before/after screenshots**, same real render, same fixture data:

- Before (10.5px): `cc2079_before.png`
- After (14.5px): `cc2079_render.png`

The size difference reads clearly and every row stays legible and uncramped.

**FOUNDER GLASS CHECK, not done here** (this container is network-blocked from scorr.in): Arpit
opens the Custom Screen Builder on scorr.in and confirms the size increase reads right on the live
page, matching the two screenshots attached to this task.
