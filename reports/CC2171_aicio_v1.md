# cc#2171 — AICIO V1: full-screen chat-like shell on /m/aicio, three live test cards, chat is version 2

Sprint AICIO_V1 (Fable-owned; founder voice note 17-Sep-2026 ~12:05 IST). Built 17-Sep-2026, 12:47–13:00 IST.

## What shipped

| File | Change |
|---|---|
| `mobile/aicio.html` | Rewritten. Shell header and bottom nav kept byte-for-byte (theme boot lines and the `bnav` block are spliced from the previous file). The body is one column sized by script to the space between the sticky header and the fixed nav: top part = a scrollable chat canvas with ONE assistant bubble, bottom part = the cards dock (snap rail + dot pager + composer). Every class is `ai-*` (the shared `mobile_app.css` restyles short names such as `.c`, `.v`, `.row`, `.chip`, `.more`, `.rail`). |
| `aicio_app_mobile.py` | Additive block: `AICIO_CARDS` registry, three builders, `build_cards()`, and `GET /api/mobile/aicio_app/cards`. The cc#1904 endpoint `GET /api/mobile/aicio_app` is untouched. |
| `tests/test_aicio_cards.py` | Five no-DB tests: registry order and shape, Indian-grouped rupees, failure isolation (raise + timeout), the three builders over stubbed source payloads, the endpoint's available/total counts. |
| `reports/CC2171_aicio_v1.md` | This report. |

## The page

- **Bubble (top).** `AICIO` eyebrow, "I am your AI CIO. Here is what the market looks like right now.", then the as-of line: server time in IST (from the endpoint's `as_of`) and the feed state from `rail_state` ("Feed live · tick 3 min ago", dot green for LIVE, red for STALE, grey for CLOSED). When a card fails the bubble adds "N of M cards available." plus the card name and a short reason (exception class stripped: "Market Mood: feed dead"). No fake history, no placeholder turns.
- **Rail (bottom).** Same geometry as `mobile/sector.html` `.rl`/`.tc`: cards `flex 0 0 78vw`, `max-width 320px`, snap-x, dot pager that follows the scroll. Each card is an `<a>` to its full page: eyebrow (title), one headline, two supporting lines, then two small lines — source and "as of …". A card with `available:false` is hidden from the rail.
- **Composer.** A read-only text field with the placeholder "Ask AICIO — chat lands in version 2" and a 44px `Type` button. Tapping Type focuses the field and shows a one-line toast "Chat is version 2. The cards are live." for 2.2 s. Nothing is sent; the harness confirms zero API requests on tap. `readonly` + `inputmode="none"` was chosen over `disabled` because a disabled input cannot take focus in any browser, and the spec asks the button to focus the field; a read-only field focuses without raising a keyboard.
- **Endpoint down.** The bubble says "I could not reach the market data right now." with a Try again link; the rail shows a dashed "Cards unavailable." placeholder (no chevron, no numbers).
- **Tokens.** 0 literal `var(--x, …)` fallbacks in the file. Body carries `min-height:100dvh; background:var(--field)` so the html fallback colour never shows under a short page on the light theme.

## Harness (Playwright, real Chromium, `scratchpad/cc2171_test.py`) — ALL PASS at 375×812 and 390×844, goldnight + aquawhite

| Measure | 375 | 390 |
|---|---|---|
| header bottom → nav top (the canvas) | 68 → 754 (686 px) | 68 → 786 (718 px) |
| bubble | 80–200, 302 px wide | 80–200, 315 px wide |
| dock (rail + dots + composer) | 534–754, 220 px = 29.1 % of the canvas | 566–786, 220 px = 28.0 % |
| cards | 3 × 293 × 131 px, dots 3, first on | 3 × 304 × 131 px |
| Type button | 60 × 44 px | 60 × 44 px |
| sideways scroll | scrollWidth 375 = innerWidth | 390 = 390 |
| vertical body scroll | document 812 = viewport | 844 = 844 |

Checked in every run: bubble text, as-of line ("17 Sep 2026 · 12:49 IST", "Feed live · tick 3 min ago"), registry order and hrefs (`/m/home`, `/m/v10`, `/m/v8`), headlines equal the payload's, no card text cut off (every headline, line and source line fits), composer read-only with the V2 placeholder, Type → field focused + toast visible above the nav + toast gone by itself + no API request, swipe → pager dot 2 on, the 2-of-3 payload → 2 cards + 2 dots + "2 of 3 cards available. Market Mood: feed dead", HTTP 500 → honest bubble + Try again reloads 3 cards, no page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2171_*.png`): 375 dark and light, 390 dark and light — the bubble sits top-left under the header with the gold/aqua accent bar, the middle of the canvas is empty by design (V1 has one message), the Market Mood card fills the rail with the Index Intel card peeking at the right edge, three dots, the composer and the nav under it; light theme has no dark band. `375_dark_card2` — rail swiped to Index Intel, dot 2 lit, the V8 card peeking with its red headline. `375_dark_2of3` — the bubble carries "2 of 3 cards available. Market Mood: feed dead", rail shows two cards and two dots. `375_dark_down` — "I could not reach the market data right now. Try again" and the dashed placeholder.

## The endpoint

`GET /api/mobile/aicio_app/cards` → `{version, as_of "YYYY-MM-DD HH:MM IST", feed{state,age_min,why}, available, total, cards[], basis}`.
Each card: `{key, title, href, source, available, headline, lines[2], as_of, raw, built_ms}` or `{key, title, href, source, available:false, reason}`.

- Cards come from ONE list, `AICIO_CARDS = [{key, title, builder, href, source}]`, in registry order; the next five are an append.
- Every builder calls the web's own function in-process, never a second SQL: Market Mood → `v8_endpoints.market_mood()` (the function behind `/api/v8/market_mood`); Index Intel → `v10_endpoints.v10_signal("NIFTY50")` and `("BANKNIFTY")` (behind `/api/v10/signal`); V8 open book → `v8_book_canon.book_canon(conn, era="fresh")` + `v8_era.era_block(cur)`, the same call `/api/mobile/v8book` makes at `mobile_ext.py:897`.
- Builders run in a thread pool; one that raises or overruns `CARD_TIMEOUT_S` (8 s) comes back `available:false` with the reason and the others are untouched (unit-tested with a raising and a sleeping builder at a 0.5 s timeout).
- NULL never prints as 0: `_inr(None)` → "—"; a missing ADR prints "ADR — (breadth feed down)"; a missing price prints "— (no closed bar yet)".
- `feed` is `rail_state(last 10-minute bar of the Index Intel card, 10, now, is_trading_day)`.

## Side by side — card text vs source payload

Sources fetched from production at 12:55 IST on 17-Sep-2026 (MCP `v8_market_mood`, `v10_signal`) and pushed through the real builders (the mapping code is the committed one):

| Card | Source payload (production, 12:55 IST) | Card text the builder produced |
|---|---|---|
| Market Mood | mood Neutral · checks ADR 4.676 pass, Nifty Day +0.52 pass, Nifty Week −0.40 fail, Nifty Month −3.91 fail · buy_slots 12 · sell_slots 9 · adr_detail 173 / 37 · nifty_source live_intraday · checked_at 2026-09-17 | **Neutral** / "2 of 4 checks passed · ADR 4.68" / "12 buy / 9 sell slots · A/D 173/37" / as of 2026-09-17 · Nifty from live intraday |
| Index Intel | NIFTY50 price 23346.8, st_dir up, signal FLAT, as_of 2026-09-17 12:50:00 · BANKNIFTY price 56313.8, st_dir up, signal FLAT | **NIFTY 23,347 · up** / "NIFTY50 23,347 · trend up · signal FLAT" / "BANKNIFTY 56,314 · trend up · signal FLAT" / as of 2026-09-17 12:50:00 · latest closed 10m bar |
| V8 open book | `book_canon(conn, era="fresh")` — the sandbox has no database, so this row is the live check below. Test fixture: long 9 + short 3, unrealised −12450.5, realised 284310, win_rate 58.3, decided 96, era "Fresh era since 18 Jul 2026" | **−₹12,450** / "12 open · unrealised −₹12,450" / "realised ₹2,84,310 · win rate 58.3% of 96 decided" / as of Fresh era since 18 Jul 2026 · live CMP at HH:MM IST |

Earlier fixture (12:49 IST, the harness payload): mood Neutral, 2 of 4, ADR 3.98, 12 buy / 8 sell, A/D 167/42 — the founder's 12:04 screenshot of the Home Market Today card showed the same gate (NEUTRAL, 2 of 4 checks failed, ADR 3.98, A/D 167/42).

## Live checks (for Fable's verification — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/aicio_app/cards` → `total 3`, `available 3`, keys in order `market_mood, index_intel, v8_book`, every card with `as_of` and `source`.
2. Same minute: `https://scorr.in/api/v8/market_mood` — `mood`, `fails`, `buy_slots`, `sell_slots`, `adr_detail` must match card 1's headline and lines.
3. `https://scorr.in/api/v10/signal` — NIFTY50 / BANKNIFTY `price`, `st_dir`, `signal`, `as_of` must match card 2.
4. `https://scorr.in/api/mobile/v8book` — the summary block's open count, unrealised, realised, win rate and the era caption must match card 3 (`raw.open`, `raw.unrealised`, `raw.realised`, `raw.win_rate`, `raw.era_label`).
5. `https://scorr.in/m/aicio` on a phone: bubble on top, rail with three cards in the bottom quarter, swipe → dots move, Type → toast, no sideways scroll.

## Not in this card (by spec)

No chat wiring, no model call, no `max_chats` write, no change to `scorr_chat_endpoint.py`, `v8_endpoints`, `v10_endpoints`, `v8_book_canon` or the web `/cio`. The V2 routing (Gemini test / Haiku fallback → DB lookup → Claude Sonnet, per the founder) is recorded in the Fable Room for the next card and is not started here. The remaining five cards wait for the founder to finalise the card set; each is a registry append.
