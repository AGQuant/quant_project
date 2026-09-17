# cc#2192 — /m/fpc option chips: a chosen option is a solid brand fill

Built 17-Sep-2026 (founder 15:02 IST); the landing time is in the task row. Page only: `mobile/fpc.html`, CSS plus one small boot script; option logic untouched.

## What changed

- One rule for every option grid on the page (`.qo span.on`) and the tap rows of step 1 (`.seg span.on` — dependants, goal): solid `--brand` fill, `--brand` border, a small lift (`box-shadow` mixed from the brand token), 120 ms transition on fill, text, border and shadow. Unchosen chips keep the pale `--hi` fill and muted text.
- The label colour on the fill is a token chosen at boot: whichever of the theme's `--field` and `--ink` reads with the higher contrast against `--brand` (`--fpc-on-fg`, set on `body`). The card asked for white text; on the purple set the brand is a pale lavender (185, 140, 255) where white reads at 2.6:1, and on Aqua White the brand is a light cyan where white reads at 1.5:1 — so those two take the dark text token (7.4:1 and 11.2:1), while the gold set takes its dark field colour (9.4:1). Every chip stays a solid brand fill; only the label token flips. One line from the founder overrules this if he wants white regardless.
- Tokens only: no literal colour anywhere (the theme ratchet stays level — the file has zero literal fallbacks and the meta theme-color is the only hex).

## Harness (Playwright, real Chromium, `scratchpad/cc2192_test.py`) — ALL PASS at 375×812 on goldnight, aquawhite and electricviolet

- Step 1: the chosen dependants / goal options are brand-filled with the better-reading text token.
- Step 2: one option tapped per question (a different column each) → all 7 chosen chips have `background == --brand`, `border == --brand`, a shadow, and the label token above; unchosen chips unchanged (`--hi` fill, muted text).
- Label contrast on the fill: 9.4:1 (gold), 11.2:1 (aqua), 7.4:1 (violet); transition 0.12 s on all four properties.
- No page errors on any theme.

**Screenshots looked at** (`scratchpad/cc2192_*.png`): `375_violet_chips` — YOUR COMFORT with questions 3–7, one solid lavender chip per question with dark bold text and a soft shadow, the others dark outlined; `375_dark_chips` and `375_light_chips` — the same on gold and on aqua.

## Live check (Fable — the sandbox cannot reach scorr.in)

`https://scorr.in/m/fpc` on the phone, any theme: tap an option → it fills solid; the others stay pale.
