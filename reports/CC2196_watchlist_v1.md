# cc#2196 — Watchlist V1: up to 5 user-named lists, a "+" on every stock card, /m/mywatchlist as swipeable list cards with a names sheet

Founder 17-Sep-2026 15:17 IST (voice + screenshot of the old "no store" page). Built 17-Sep-2026 from 17:33 IST server time; the landing time is in the task row.

## Data-source gate answered first

`auth_sessions` carries **no user id** — its columns are `token, created_at, expires_at` (one app password, per-login tokens, `scorr_auth.py`). There is no per-user identity anywhere in the app today, so the store is keyed on `user_id = 'founder'` (a real column, so nothing changes shape the day a login carries a user id). Stated, not assumed.

## What shipped

| File | Change |
|---|---|
| `watchlist_app_mobile.py` | NEW (replaces `mobile_watchlist_stub.py`, deleted). STORE, CREATE-only and idempotent at first use (the cc#2095 pattern, no ALTER/DROP): `user_watchlists (id, user_id, name, position, created_at)` + a unique index on `(user_id, lower(name))`; `user_watchlist_items (watchlist_id, symbol, added_at, added_from, gvm_at_add, price_at_add)` with primary key `(watchlist_id, symbol)` and `ON DELETE CASCADE`. The max-5 guard is in the API under a per-user `pg_advisory_xact_lock` (a CHECK cannot count rows); a 6th list gets HTTP 409 "You already have 5 watchlists — the most allowed. Delete one to add another." API (all `_guard`): `GET /api/mobile/watchlists` (lists + counts + symbols + avg day % + top mover), `POST` (create), `PATCH /{id}` (rename), `DELETE /{id}`, `GET /{id}/items` (rows joined with the latest `gvm_history` rating, CMP from `cmp_prices` with the latest close as fallback, day % vs `prev_close.prev_session_close_many` — the anchor custom_alerts and get_v8_live_metrics use — plus added-on, `gvm_at_add`, `price_at_add`, % since add and the GVM delta; sorted by day % desc), `POST /{id}/items {symbol, surface}` (captures `gvm_at_add` / `price_at_add` at insert; idempotent, `already: true` on a repeat), `DELETE /{id}/items/{symbol}`. Pages `/m/mywatchlist` (the My Scorr tile's route, kept) and `/m/watchlist` (the card's name, same page). Pure `shape_lists` / `shape_items` / `validate_*` / `capture_at_add`. |
| `scorr_watchlist_add.js` | NEW shared component (site-wide via `_MOBILE_HEAD`): `ScorrWatchlistAdd.button(sym, from)` renders the **+**, a capture-phase tap on any `[data-wl-add]` opens the bottom sheet (the user's lists with counts and an "already here" tick, a **＋ New watchlist** row while under 5 with an inline name field, "Open My Watchlist ›"), tap a list → `POST items` → toast "SYM added to Trading watchlist." → the + becomes a **✓** wherever that symbol is on screen (one `GET /api/mobile/watchlists` per page, refreshed after every add; a MutationObserver paints buttons rendered later). Overlay above the symbol card sheet (z 100500). Tokens only, no colour literals. |
| `scorr_card_strip.js` | On the app the shared C·A·R·D strip carries the + as its **fifth control** (after D), so every stock card that shows the strip has it with no page code — V8 signal rows and TC Scanner rows (the tap-reveal strip, `scorr_position_row.js`), the GVM company view (`fillStrip`), and the app-wide symbol card sheet that opens on any tapped symbol (`scorr_card_common.js`). Not a letter: it never reaches `ScorrCardNav`. The web strip is unchanged. |
| `mobile/invscan.html`, `mobile/results.html`, `mobile/screeners.html`, `mobile/sector.html` | Explicit + on rows that have no strip: Investment Scanner rows (under the score, inside the row link — the capture-phase tap never follows the link), Results movers cards, Screener table rows (first cell), the Sector companies table (first cell of company rows; segment rows have no symbol, so no button). |
| `mobile/mywatchlist.html` | REWRITTEN. Header "My Watchlist" + "n of 5 lists · m names" + **＋ New** (disabled at 5). A horizontal swipeable rail of list cards: name, "Watchlist · n names", "avg day +x.xx% · top SYM +y.yy%" (or "empty — tap + on any stock card"), the first symbols, "Open ›"; tap → a **full-height sheet** with the names table Stock (company) · Rating (verdict) · CMP · Day · Added (% since), sorted by day %, **swipe a row left → Remove**, tap a name → its check page; hold a card or ⋯ → **Rename / Delete** (delete asks twice). Empty state: "No watchlists yet — tap + on any stock card" + the New button. The NAMES SAVED / STORE / WRITE PATH card and all explainer text are gone. Tokens only (the old page's white-on-light values were its `--txt`/`--blu` web-token reads; this page reads the contract tokens). |
| `main.py`, `pwa_endpoints.py` | Wiring: the router swap, `PROTECTED /m/watchlist`, `NAV_REGISTRY` entries, the `_MOBILE_HEAD` script tag + the cache-stamp list (cc#1060 rule), the `/scorr_watchlist_add.js` route. |
| `tests/test_watchlist_app_mobile.py` | 5 tests: constants + CREATE-only DDL; name/symbol validation; `shape_lists` counts, avg day % and top mover; `shape_items` sort, since-add % and GVM delta with nulls kept; `capture_at_add` reads the latest gvm_history score and the live CMP with the close fallback (stub cursor). 5 passed (15 with the other two new suites). |
| `reports/CC2196_watchlist_v1.md` | This report. |

## Every surface where + is mounted (spec item 3, printed)

| Surface | How |
|---|---|
| /m/v8 signal rows | the C·A·R·D strip (tap-reveal row strip) |
| /m/tcscan rows | the C·A·R·D strip (tap-reveal row strip) |
| /m/gvm company view | the C·A·R·D strip (`fillStrip`) |
| any tapped symbol app-wide (the symbol card sheet) | the C·A·R·D strip inside the sheet |
| /m/invscan board + enters rows | explicit, under the score |
| /m/results movers (Biggest profit jumps / falls) | explicit, in the GVM line |
| /m/screeners result rows | explicit, first cell |
| /m/sector companies table | explicit, first cell of company rows |

## Harness (Playwright, real Chromium, `scratchpad/cc2196_test.py`) — ALL PASS at 375×812, goldnight + aquawhite, plus 360×780

Stateful mocks of all seven endpoints; the list and item payloads are produced by the shipped `shape_lists` / `shape_items` over fixture prices read from production on 17-Sep (RELIANCE 1,243.9 vs prev 1,230.0 · TCS 2,190 vs 2,210 · INFY 1,543.2 vs 1,520) and the latest gvm_history rows.

- Empty state: "No watchlists yet — tap + on any stock card", "0 of 5 lists · 0 names". ＋ New → "Trading watchlist", then "Investment watchlist" → two cards on the rail, "2 of 5 lists · 0 names".
- **/m/tcscan**: tap the RELIANCE row → the strip shows C · A · R · D · **＋** (32 px, `data-scorr-skip`); tap + → the sheet (z 100500) lists both watchlists and the New row; tap Trading → `POST /api/mobile/watchlists/{id}/items {symbol: RELIANCE, surface: strip}` → toast "RELIANCE added to Trading watchlist." → the + is now ✓.
- **/m/invscan**: every board row carries the +; tap TCS's + → the sheet opens and the row link is NOT followed; Investment → TCS added (surface invscan).
- **Symbol card sheet** (a `.sym` element on a test page, the app-wide path): the strip inside the sheet carries the +; the add sheet marks the list TCS is already on ("already here" + ✓); Trading → TCS added.
- **/m/results**: every movers card carries the +.
- **Rail**: Trading "Watchlist · 2 names", "avg day +0.11% · top RELIANCE +1.13%", "TCS · RELIANCE"; Investment "1 name"; sub "2 of 5 lists · 3 names". Tap Trading → the full-height sheet (747 px): RELIANCE 4.8 Weak · ₹1,243.9 · +1.13% · 17 Sep 26 · 0.0% since; TCS 5.4 Weak · ₹2,190 · −0.90%; sorted by day %; footer "2 names · sorted by day % · 2 priced · swipe a row left to remove · tap a name for its check page". Swipe TCS left → Remove appears → tap → `DELETE …/items/TCS` → the row is gone, the card reads "1 name".
- ⋯ → Rename → PATCH, the card shows "Sell watchlist". Three more lists → 5 cards, **＋ New disabled**, the rail scrolls, the page does not; forcing a 6th → "5 of 5 watchlists used — delete one to make room." ⋯ → Delete asks twice → DELETE → 4 cards. 360 px: no overflow. No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2196_*.png`): `375_dark_rail` — "MY WATCHLIST · 2 of 5 lists · 3 names", the gold ＋ New, the Trading card with "avg day +0.11% · top RELIANCE +1.13%", "TCS · RELIANCE" and "Open ›", the Investment card peeking at the right, the hint and the note. `375_light_sheet` — the full-height names sheet on aquawhite: "WATCHLIST · 2 NAMES / Trading watchlist", the table with RELIANCE (4.8 Weak, ₹1,243.9, +1.13% green, 17 Sep 26) and TCS (5.4 Weak, ₹2,190, −0.90% red). `375_dark_tcscan_sheet` — the TC Scanner page dimmed under "ADD TO A WATCHLIST / RELIANCE", the two list rows, "＋ New watchlist · 2 of 5 used", "Open My Watchlist ›". `375_light_invscan_sheet` — the Investment Scanner rows each with a + under the score, and the sheet for TCS showing Trading "1 name".

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `information_schema`: `user_watchlists` and `user_watchlist_items` exist (CC creates them with the same DDL right after the deploy; the app also creates them at first use).
2. On the phone: /m/tcscan → tap a row → the strip ends in +; tap → the sheet; New watchlist "Trading" → the symbol lands; `run_sql`: `SELECT symbol, added_from, gvm_at_add, price_at_add FROM user_watchlist_items` shows the captured rating and price.
3. /m/mywatchlist → the rail with the count; tap → the names sheet with rating + CMP + day %.

## Out of scope

Rating / price engines untouched. Card layouts untouched beyond the + button. A per-user identity (the day logins carry one, `USER_ID` becomes a lookup — one line).
