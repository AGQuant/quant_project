# cc#2188 — /m/options: "+ Add leg" is a full-width button above the legs; each leg on two lines with 44 px controls

Built 17-Sep-2026; the harness passed at 15:18 IST server time; the landing time is in the task row. Page only: `mobile/options.html`. The shared `option_strategy.js` (web + app) is untouched, so the web /options page keeps its plain layout.

## What shipped

| File | Change |
|---|---|
| `mobile/options.html` | The shared script's own `#optAddLeg` control moves out of the hint line under the table into `.opt-addbar` ABOVE the legs card: one full-width button, 48 px tall, brand outline. Its text ("+ Add leg (N of 10)") and its disabled state at 10 legs still come from the shared script, unchanged. The table header is Side · Type · Strike. Each leg is now two lines: line 1 = side pill · type pill · strike control · ×; line 2 = Premium (captioned) · Lots stepper (captioned, − value +). Every pill, input, stepper button and × is at least 44 px tall. The hint sentence stays under the table: "Gold premium = from chain. Blank = no chain row. 1 lot = 75 units." (the lot size from the page's own meta). A page-local `decorate()` wraps the premium input and the lots input with their captions after every shared render (the same MutationObserver as cc#2187). Tokens only; one literal fallback fewer than the file had before (`--radius-7,8px` is gone); the only hex in the file is the meta theme-color. |
| `reports/CC2188_options_addleg.md` | This report. |

## Harness (Playwright, real Chromium, `scratchpad/cc2188_test.py`) — ALL PASS at 375×812, aquawhite + goldnight

Served the way production serves it: the page plus the `_MOBILE_HEAD` injection (legacy mobile.css, R5 theme, card common, position row, strip, bell), the option endpoints stubbed with a NIFTY chain at spot 22,450, lot 75.

- The Add leg control is a full-width button above the table with the counter: "+ Add leg (2 of 10)", 343×48 px.
- Hint sentence and lot size under the table: "Gold premium = from chain. Blank = no chain row. 1 lot = 75 units."
- Spot value uses `--ink` (cc#2187 kept).
- Every leg control is on two lines and at least 44 px tall: pills 44, inputs 44, stepper buttons 44, × 44; a leg row is 129 px; no sideways overflow.
- Line-2 captions read Premium and Lots.
- Tapping Add leg to 10 legs: counter "+ Add leg (10 of 10)" and the button is disabled; removing legs with × brings it back to "(2 of 10)".
- cc#2187 stepper flow re-run inside this layout: lots start at 1 with minus off; + three times → 4 in the input and in the shared state; − → 3; a typed 7 lands; 50 disables +; a typed 60 clamps to 50 by the shared rule.
- Calculate payoff: the request carries the stepped lots (3 + 1); tiles Break-even 23,140 and 23,760 · Max profit ₹45,000 · Max loss ₹80,600; tiles and price column use `--ink` with 15–18:1 contrast.
- No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2188_*.png`): `375_light_legs` / `375_dark_legs` — the legs card: Side · Type · Strike header, BUY · CE · 22,450 ▾ · × on the first line, "Premium 150" and "Lots − 3 +" on the second, the second leg the same with 1 lot, the hint sentence under the table. `375_dark_ten` — the full page with "+ Add leg (10 of 10)" greyed out above the card and ten legs below. `375_light_after` — the light theme after Calculate payoff: the gold-outlined Add leg button, the two legs, the hint, the cyan Calculate button and the three tiles in dark ink on pale cells.

## Observation outside this card

In the harness on the light theme the shell header title "OPTION STRATEGY" is pale on the white header bar (the light `375_light_after` picture). `mobile/theme_mobile.css` carries the cc#1668 rule that paints it `--ink` when `body` carries `data-theme`; the harness sets the theme through localStorage only. So this may be a harness artefact. Live check item 3 below settles it; if it shows on the phone it is the cc#1668 rule not firing, a shared-shell matter, not this page's.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/m/options` on a phone: the "+ Add leg (N of 10)" button sits above the legs card, full width; it greys out at 10 legs.
2. Each leg shows two lines, every control easy to tap (44 px); the − / + stepper still drives the lots and Calculate payoff still uses them.
3. On Aqua White: the header title "OPTION STRATEGY" is readable (dark ink) — see the observation above.
