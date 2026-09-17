# cc#2185 — Home grid "Index Trades" tile: a tap now opens the NIFTY / BANK NIFTY index trade log

Built 17-Sep-2026; the harness passed at 15:26 IST server time; the landing time is in the task row.

## Finding (spec item 1)

The tile in `mobile/home.html` G_GROUPS (Analytics, second-last, before QB Builder) carries `href="/m/home#index-trades"`. The page has a one-shot deep link (cc#2066) that opens the V10 log when the page LOADS with that hash. The tile sits on /m/home itself, so a tap only changed the hash: no load, no listener, nothing opened. That is the whole bug.

## Where the log lives (spec item 2)

No mobile page renders the index trade log. /m/v10 (Index Intel) is the live desk and has no trade log at all. The log is the `#v10ov` modal on /m/home — the one the Market-Today Index Positions card (card 4) opens with VIEW LOGS: `openV10(event,'NIFTY50','OPT',true)` → options view, NIFTY 50 first, BANK NIFTY pill, leg switch hidden. So the tile now opens exactly that, on the page, with the same call. No new page, no new route, no second copy of the log.

## What shipped

| File | Change |
|---|---|
| `mobile/home.html` | The tile keeps its href (a fresh navigation from any other page still deep-links) and gains `onclick: "openV10(event,'NIFTY50','OPT',true)"`; `gtileR1()` takes an optional tap handler and writes it on the anchor. A `hashchange` listener opens the log when a same-page link sets `#index-trades` (the More sheet, any in-page anchor). `closeV10()` drops the hash on close (replaceState, no history entry) so the next such link counts as a change again; closing never re-opens anything. |
| `main.py` | NAV_REGISTRY mirror: `/m/home#index-trades` → "Index Trades (mobile) — Home grid Analytics tile; opens the V10 index trade log …", tier `grid-tile` (same key style as `/dashboard#index`). Wiring only. |
| `app_route_map` (DB) | New row `/m/home#index-trades`: label Index Trades (mobile), surface mobile, in_nav false, home_grid_group Analytics, linked_from "home grid (Analytics tile; opens the V10 index trade log modal on /m/home …)", route_group engine, serving_file mobile_endpoints.py, handler m_home, template mobile/home.html, crawl false, added_by claude_code, added 15:26 IST. Printed below. |
| `reports/CC2185_index_trades_tile.md` | This report. |

PWA inject and PROTECTED: /m/home is already in both; nothing to add. HOME_GRID_R1 tile order and the V10 engine are untouched (do_not_touch).

## One query, two readers (spec item 4)

Card 4 (`loadHeroPositions()`) and the log (`loadV10()`) both fetch `/api/mobile/v10chart?symbol=…` (`mobile_home2.mobile_v10chart`) and read the same `trades[]`. The card shows the OPEN legs; the log shows the closed option legs (cc#973) plus the "N closed · N open" line and the chart's open marker. Same payload, so the numbers cannot drift.

## app_route_map row (spec verify item 3)

```
route                 label                  in_nav  home_grid_group  linked_from
/m/home#index-trades  Index Trades (mobile)  false   Analytics        home grid (Analytics tile; opens the V10 index trade log modal on /m/home -- same modal as the Index Positions card VIEW LOGS; no page of its own)
```

## Harness (Playwright, real Chromium, `scratchpad/cc2185_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Served as production serves it (page + the `_MOBILE_HEAD` scripts); `/api/mobile/v10chart` stubbed with one book per index in the endpoint's own shape (NIFTY: closed 24,800 CE +₹6,750, open 25000 CE bought 95.50; BANK NIFTY: closed 56,400 PE +₹5,950, open 56000 PE bought 310); every other API `{}`.

- The tile sits in the Analytics grid with the href and the tap handler; grid order unchanged (… Baskets, Index Trades, QB Builder); the log is closed on a plain load.
- Tap → `#v10ov` opens, the URL stays clean, body scroll locks; NIFTY 50 pill on, options view, leg switch hidden; the tap fetched v10chart for NIFTY50 (bars=360).
- NIFTY log populated: the 24,800 CE card, "1 closed · 1 open", footer "1 option trade", strip 1 TRADES / +₹6,750 GROSS / +₹6,750 NET.
- Card 4 shows the same open NIFTY option the log's payload carries: "NIFTY · OPTIONS 25000 CE · bot 95.50 … 104.20 now +₹653" (same fixture, same endpoint).
- BANK NIFTY pill → its own book (56,400 PE card, 1 open, net +₹5,950), the NIFTY card gone.
- Close → closed, scroll back; a second tap opens it again.
- `/m/home#index-trades` from another page still opens on load (cc#2066 kept); close clears the hash; setting the hash again on the same page opens it via hashchange.
- No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2185_*.png`): `375_dark_tile` — the Home grid with ANALYTICS … Planning · Baskets · Index Trades · QB Builder, then MY SCORR, the five-item bottom bar. `375_dark_modal` — "NIFTY 50 · V10 trades", the NIFTY 50 pill lit gold, "1 closed · 0W/0L · 1 open  Net ₹6,750", the 5-min chart, TRADE LOG strip, SEP 2026 group with "24,800 CE BUY · Bullish · TARGET · 120.00 → 210.00 premium · 09-10 → 09-11 · ₹6,750 +90.0 pts". `375_light_bank` — the same modal on the light theme after the BANK NIFTY pill: "56,400 PE BUY · Bearish · TARGET · 250.00 → 420.00 · ₹5,950 +170.0 pts".

## Observation outside this card (filed as its own card)

The modal's stat line reads "0W/0L" while the closed trade in view is a TARGET win. The line counts wins from `t.net_pnl` over the per-leg `trades[]`, but the endpoint's per-leg dicts carry `pnl` (and `win`), not `net_pnl` (`net_pnl` is a field of the PAIRED list). So the count is 0/0 for every leg view on production too. Not touched here; a new card carries it.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. On the phone, /m/home → Analytics → Index Trades: the log opens at once, NIFTY 50 first; BANK NIFTY pill switches the book; close, tap again, it opens again.
2. Card 4 (Index Positions) and the log agree on the open position for each index (one endpoint).
3. `SELECT * FROM app_route_map WHERE route='/m/home#index-trades'` → the row above.
