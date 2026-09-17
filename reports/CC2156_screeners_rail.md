# cc#2156 -- Screeners app PUSH 1/5: ready-made screen cards go horizontal, one snap rail per group

Founder screenshot 17-Sep 11:20 IST of /m/screeners: a 3-column grid squeezed each screen into a tall
narrow box with a 5-line rule. Now each group (Momentum & breakouts / Quality & growth / Value) is ONE
horizontal snap-scroll rail, one card per screen, swipe for the next, dot pager under each rail.

## 1. Backend (additive) -- `screeners_app_mobile.py`, `/api/mobile/screeners_app/list`

Each card gains `top: [symbol, ...]` (at most 3) from one grouped query:
`ROW_NUMBER() OVER (PARTITION BY screen_id ORDER BY rank NULLS LAST, symbol) <= 3` over
`v13_screen_results` for the listed screen ids. Nothing else in the payload moves.

**Proof of byte-identical-minus-top** (`scratchpad/cc2156_api_test.py`): the OLD function (HEAD snapshot
of the module) and the NEW function were both run over the SAME production rows (the endpoint's own
queries, run on the DB 17-Sep 11:47 IST: 7 presets, names_today 89, the 19 top rows), replayed through a
fake cursor. `NEW minus top == OLD` holds on the dict AND on the sorted JSON string; the old payload has
no `top`; every `top` has 1-3 symbols; the new function runs exactly one extra query (3 vs 2).

| Screen | members | top |
|---|---|---|
| Momentum Kings | 10 | SPECTRUM, LOTUSDEV, ACCENTMIC |
| 52-Week Breakout | 3 | ACCENTMIC, SPECTRUM, DIACABS |
| Quality Compounders | 30 | CUPID, MODISONLTD, AEROFLEX |
| Market Leaders | 8 | BHEL, POWERINDIA, MOTHERSON |
| Future Giants | 1 | NYKAA |
| Multibagger Hunt | 24 | BHAGYANGR, MODISONLTD, TBZ |
| Hidden Value | 21 | ARVSMART, WEBELSOLAR, RAMCOIND |

`as_of` 2026-09-16, key_metrics `{screens: 7, names_today: 89, stale: 0}` -- unchanged.

## 2. Page -- `mobile/screeners.html`

- `renderList()`: per group, `<div class="sk-rail">` + one `<a class="sk-card">` per screen + a
  `<div class="sk-pager">` with one dot per card. Rail geometry copied from `mobile/sector.html` `.rl/.tc`
  (flex, gap 10px, padding 4px 16px, `scroll-snap-type:x mandatory`, hidden scrollbar), plus
  `scroll-padding-left:16px` so the snap lands on the 16px gutter. Cards `flex:0 0 86vw; max-width:360px;
  scroll-snap-align:start`.
- Card, horizontal, left-aligned: 44px icon disc (24px glyph) on the left; right column = name (type-14,
  800, one line), members line (`10 names` + ` · ran 12 Sep` when stale), the rule on ONE line with an
  ellipsis (the full rule lives on the screen page), then the preview strip of up to three symbol chips.
  Stale dot top-right (green latest / red stale) unchanged.
- Equal heights: cards are `align-items:stretch` flex items in the rail and carry `min-height:118px`, so a
  1-name screen and a 30-name screen line up.
- Dot pager: purely visual, `scroll` listener on the rail (rAF-throttled) -> active dot =
  `round(scrollLeft / (card 2 offset - card 1 offset))`. No state, no URL.
- Key-metrics card, the as-of line, the Build-your-own wide card and the note stay. The old
  `.grid/.sc` CSS stays only for that wide card (push 4 rewires it).
- **Class names are `sk-*` on purpose.** The spec suggested `class=rail`; `/static/mobile_app.css`
  (`mobile_endpoints.MOBILE_CSS`) has BARE `.rail` (line 96, `display:inline-flex` + mono font), `.sc`
  (line 320, `margin-left:auto` + mono font), `.chip` and `.body` rules -- the cc#2155 root cause. The
  harness asserts the card carries no leaked margin or mono font.
- Contract tokens only; `theme_validate mobile/screeners.html`: raw 0, baseline 0, clean; no
  `var(--x, literal)` fallbacks added (grep 0).

## 3. Checks -- `scratchpad/cc2156_test.py`, `=== ALL cc#2156 PAGE CHECKS PASS ===`

Real `mobile_app.css` + `scorr_themes.css` + `scorr_appshell.css`, the list payload = the NEW endpoint's
real output on the production rows above. Four runs: dark (goldnight) and light (aquawhite) at 375px and 390px.

| Check | 375px | 390px |
|---|---|---|
| three rails, cards per rail | 2 / 3 / 2 | 2 / 3 / 2 |
| first card at the snap position | left 16, right 339 (w 323 = 86vw) | left 16, right 351 (w 335) |
| equal heights within a rail | 118 / 118 / 118 on every rail | same |
| rule on one line | nowrap, single line, ellipsis where longer | same |
| preview chips per card | 1-3 | 1-3 |
| dots per rail = cards, first active | yes | yes |
| one snap = one card (quality rail, `scrollTo(step)`) | card 2 at left 16, right 338, dot 2 active | left 16, right 352, dot 2 |
| third card | reachable, right 359 <= 375, dot 3 active | right 374 <= 390 |
| icon disc / name size | 44x44 / 14px | same |
| no `.sc`/`.rail` leak on the card (margin 0, Sora font) | yes | yes |
| as-of line + key-metrics text identical to the shipped page | yes | yes |
| page `scrollWidth == innerWidth` (rails scroll inside themselves) | yes | yes |
| Build-your-own wide card, href `/screeners#custom` | present | present |
| page errors | none | none |

Before, for the record: the shipped grid rendered the first rule over ~6 lines at 375px (~5 at 390px).

Screenshots looked at (VISUAL_VERIFY_GATE_V1): `cc2156_before_dark_375.png` (the founder's squeeze,
plus the mono-font leak from the bare `.sc` rule), `cc2156_after_goldnight_375.png`,
`cc2156_after_aquawhite_375.png`, `cc2156_after_dark_390_swiped.png` (Market Leaders snapped in, dot 2
active). Each rail shows one full card with the next card's edge peeking at the right, three chips under
the one-line rule, the pager centred.

## 4. Found, not fixed (outside this push)

- The key-metrics values (7 / 89 / 0) render as dark bordered boxes: the bare `.v` card rule in
  `mobile_app.css` (line 1305), same leak cc#2155 fixed on /m/sector. The spec says the key-metrics card
  stays exactly as it is this push -> noted for cc#2166 (its item 3 lists other pages).
- The Build-your-own wide card text is mono: the bare `.sc` rule (line 320). Push 4 (cc#2159) rewires
  that card; the new class should be `sk-*` too.

## 5. Live check after deploy

- https://scorr.in/api/mobile/screeners_app/list -> every card has `top` (<= 3); the rest as before
- https://scorr.in/m/screeners -- dark + light: three rails, swipe moves one card, dots follow
