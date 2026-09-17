# cc#2179 + cc#2172 — Trade Check and GVM hero cards: plain white panel on the light themes, hairlines, bordered tiles

Built 17-Sep-2026 (founder 12:16 and 12:27 IST); the harness passed at 16:43 IST server time; the landing time is in the task rows. One convention, one commit, one shared file: `mobile_endpoints.py` (the `MOBILE_CSS` blocks `#ckp` and `#gvp`). The pages themselves are untouched.

## The decision (needs_reco 7037, decided under the founder's 17-Sep standing instruction)

Both cards said "background becomes `--panel`" and "dark themes must render identically to before". On the dark sets the hero sits on the shell's sunk fill (`--sunk`, darker than `--panel`), so a blanket `--panel` would have changed them. Decision: the plain panel applies on the **light sets only**; dark sets keep their hero. The light-set list is derived, not guessed — every `[data-theme]` set in `scorr_themes.css` whose `--field` luminance is above 0.5 (the page boot script's own colour-scheme test): aquawhite, blush, duskday, goldday, orangepeel, silvergold. The other nine are dark. Fable may overrule after commit.

## What changed (all rules scoped by `body[data-theme="<light set>"]` unless said otherwise)

**Trade Check (`#ckp .c-hero.pressed`, cc#2179):** the hero on `--panel`, no inset shadow. The four bucket tiles already sit on `--well` (= `--hi` on this page) with a `--line` (= `--edge`) border, so item 2 held already; the selected tile keeps its brand border. The Run Scan and Invest Check tabs do not share the pressed hero, so they are untouched (item 3). Verdict word, score and bar untouched.

**GVM (`#gvp #score`, cc#2172):**
- (2) one `--line` hairline above each of the dial+pillars block, the verdict+punchline block and the three tiles, with 12 px above — **on every theme**.
- (1) the hero on `--panel`, no inset shadow — light sets.
- (4) the three tiles on `--hi` with a 1 px `--edge` border instead of the well shadow — light sets.
- (3) company name and the CMP line in `--muted`; the punchline at line-height 1.58 — light sets.
- Found while looking at the first light screenshot: the `#gvp` block hard-codes its `--chalk` text token as near-white (its text on the sunk fill). On a white panel the symbol, price, pillar values, punchline and tile values vanished (the card's own "make the text appealing" ask, and the reason the founder saw pale text on the lavender fill). Inside the hero on the light sets `--chalk` now means the theme's `--ink`, and the symbol / price / pillar values / plain tile values carry `--ink` explicitly. The coloured trend word keeps its own colour.

Tokens only; no literal anywhere in the new rules.

## Harness (Playwright, real Chromium, `scratchpad/cc2179_test.py`) — ALL PASS at 375×812

Each page is served twice per theme: once with the shared CSS as it was on `main` before this change, once with the new CSS. On **goldnight** the computed styles (background, shadow, colour, font size, line height, borders, padding, margin) of the hero and every descendant are compared before vs after; on **aquawhite** the new rules are asserted.

- GVM dark: hero + 80 descendants identical before/after apart from the three hairlines (1 px, 12 px above); the sunk fill and its shadow exactly as before.
- GVM light: hero background = `--panel` (255,255,255), was the sunk (228,238,242); no shadow; 3 tiles on `--hi` with a 1 px `--edge` border and no well shadow; company name muted; punchline line-height 1.58; symbol, price, punchline and tile values in `--ink` (14,26,32).
- Trade Check dark: hero + 44 descendants identical before/after; fill and shadow exactly as before.
- Trade Check light: hero background = `--panel`, no shadow; 4 bucket tiles — 3 unselected on `--hi` with a 1 px border, the selected one with its own fill and 1 px brand border; verdict "Valid", score 61.0 / 100, bar 61 % untouched.
- No page errors in any run. (Harness note: the GVM page is served with the sections below the hero left out, because they need the full company payload; the hero is rendered by the page's own `renderHero()` on an endpoint-shaped fixture.)

**Screenshots looked at** (`scratchpad/cc2179_375_*.png`): `light_gvm` — PNB on Aqua White: white hero, PNB and ₹104.3 in dark ink, the three chips, a hairline, the gold 6.4 dial with the three pillar rows (5.9 / 7.8 / 5.6 in ink), a hairline, "Good · TOP 34% OF 1,773" with the punchline in ink, a hairline, the three tiles boxed. `dark_gvm` — the same on Gold Night: the sunk hero exactly as before with the three hairlines added. `light_check` — BAJAJ-AUTO on Aqua White: the white hero, "Valid" and 61.0 / 100 with the band bar, the four bordered tiles with BUY-MOM selected. `dark_check` — unchanged Gold Night hero.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/m/gvm?sym=PNB` on Aqua White: white hero, dark text, three hairlines, boxed tiles; on Gold Night: as before plus the hairlines.
2. `https://scorr.in/m/check?sym=BAJAJ-AUTO` on Aqua White: white hero, tiles boxed; Gold Night unchanged.
