# cc#2005 — backend prerequisite: side filter on the daylog P&L series

First push toward cc#2005 (V8 open positions swipeable P&L strip). This lands the ONE backend
piece the chart-per-slice requirement (item 3/4) needs and that did not exist anywhere in the repo
— everything else in the card is frontend and follows in a later push (see "Not done" below).

## Recon first (background agent), key findings

- The card is about **`mobile/v8.html`** (the app page), not the desktop `v8_dashboard.html`.
- The current 3-box strip is `renderKpi()` (`mobile/v8.html:594-603`), 10 lines, one call site
  (`renderOpen()`). `bk.long`/`bk.short` are sub-objects already on the SAME `/api/mobile/v8book`
  payload as `bk.unrealised` — no second fetch needed for those two boxes.
- The CLOSED section the card asks to relocate is `mobile/v8.html:1197-1319`. **Correction to the
  card's own paraphrase**: it has **three** filter rows today (side / window / basket), not two —
  basket (`CL_BASKETS`, line 1241) was added later by cc#1626, after the founder's 02-Sep quote
  that only mentioned side+duration. The relocation plan needs to carry all three, not two.
- **UNREALISED is pure gross mark-to-market — no brokerage concept exists on the open side**
  (`v8_book_canon.py:207-211`, confirmed again at the per-row loop in `mobile_ext.py:842`). Traced
  fully: `bk.unrealised` -> `mobile/v8.html:STATE.book` -> `/api/mobile/v8book` ->
  `mobile_ext.py:mobile_v8book()` -> `v8_book_canon.book_canon()`.
- **REALISED already nets Rs.500/closed-trade brokerage** (`v8_book_canon.py:112,275,279` —
  `BROKERAGE_PER_TRADE = 500`, rule 13's own canon).
- `mobile/v8.html:798,1420` reference `bk.brokerage_per_trade` — **dead code**. `mobile_ext.py`'s
  payload never sets that key (it sets `brokerage`, singular, at line 995), so `num(...)` is always
  falsy there today. Noted for cleanup in the frontend push, not touched here.
- **No reusable "P&L over time, filterable by slice" chart component or endpoint exists.** The
  closest is `dlChartSvg` (`mobile/v8.html:1126-1148`, hand-rolled SVG) over `/api/v8/daylog/series`
  (`v8_daylog_extras.py`) — whole-book, closed-trades-only, **no long/short filter at all**. Item 4
  of the card only needs the REALISED cumulative curve plus the LIVE unrealised total plotted as
  one end-point marker ("realised curve; unrealised marked as the live end-point") — not a full
  historical unrealised series, which does not exist anywhere and is not what's being asked for.
  The live unrealised end-point can be read straight off the already-loaded `bk` payload client-side
  — no new endpoint needed for that half.
- The Home hero-deck swipe pattern (`home.html`) is CSS scroll-snap + a plain `scroll` event
  (`heroDots()`, no touch handlers at all) — `bookDots()` is the same thing plus tap-a-dot-to-jump.
  Noted for the frontend push.

Full recon detail is in this session's own record; citing only what this push actually touches.

## The GROSS/NET finding the card's own gate asks for (item 2: "say so... do not bury it")

**Resolved by reasoning, not by asking** — worth stating plainly rather than treated as silent:
UNREALISED shows gross-equals-net TODAY because **zero brokerage has been charged yet** on an open
position under the existing model (`BROKERAGE_PER_TRADE` applies per CLOSED trade only, rule 13).
This is not a bug the card's gate is asking to fix — an open position genuinely owes no exit fee
until it exits. So: **no number changes**. Pane 3 (Net Total) = Pane 1 (unrealised, already net of
the zero fee due) + Pane 2 (realised, already net of the real fee paid) is exact and consistent
without inventing an "anticipated future brokerage" deduction the founder never asked for and that
would arguably be less honest (charging a fee for an exit that hasn't happened, and may not happen
on the date implied). The (i)-button disclosure the founder specified will show this honestly for
pane 1: gross P&L, brokerage charged so far = Rs 0 (with a one-line note why), net = the same
figure — satisfying "say so" by explaining, not by silently changing a number nobody asked to see
changed. Flagging this reasoning here so it's overridable in one line if the founder actually did
want an anticipated-fee estimate; nothing is pushed live under an assumption I haven't stated.

## What this push lands

- `v8_endpoints.py` — `v8_daylog()` gains `side: str = None`, purely additive: a new
  `AND (%(side)s::text IS NULL OR side = %(side)s)` clause on the **closed** CTE only. Every
  existing caller (the Day Log tab itself, and the one other caller in the repo,
  `v8_daylog_extras.py`) omits it and gets byte-identical behaviour — confirmed by grep, `v8_daylog`
  has exactly one caller in the whole repo. Docstring states the one real caveat: `net_open` (and
  the opened-side counts) are NOT side-consistent when filtered, since `all_dates`/`opened` are
  deliberately left unfiltered (the Day Log table's own entry-day display must not change, and the
  new caller never reads those fields anyway). Response now echoes `"side"` for transparency.
- `v8_daylog_extras.py` — `v8_daylog_series()` gains the same `side` passthrough param;
  `build_series()`'s output now also echoes `"side"`.

## Verify

- `ast.parse` clean on both files.
- **Real-data consistency check**, run directly against production via SQL (same WHERE shape the
  new code adds — cutover `2026-07-18T06:17:59.896362`, retired baskets
  `s1_reclaim_obs`/`buy_s1_bounce`, fresh era, equity view): LONG-only + SHORT-only exactly
  reconstruct the unfiltered whole-book totals —
  - count: 84 + 78 = 162
  - gross: 38,177.20 + 264,664.50 = 302,841.70
  - brokerage: 42,000 + 39,000 = 81,000
  - net: −3,822.80 + 225,664.50 = 221,841.70
  All four exact, zero rows double-counted or dropped by the added filter.

## What this push does NOT include

Everything frontend: the 3-pane swipeable carousel (HTML/CSS/JS restructure of
`mobile/v8.html`), migrating the KPI strip and the CLOSED section's three stat boxes + three
filter rows + card list into panes 2/3, the pane-drives-list wiring (founder addition A), removing
the standalone CLOSED section (founder addition B), the (i)-button disclosure sheets, the 9
tap-to-chart targets, the chart itself (reusing `dlChartSvg` as a template, now callable per-side
via the endpoint this push adds, plus the client-side live-unrealised end-point marker), and the
`bk.brokerage_per_trade` dead-code cleanup. This is real frontend engineering against a founder
glass-check gate ("swipe 3 panes, tap each of the 9 boxes, chart opens for the correct slice") that
deserves its own careful pass rather than being rushed alongside a backend change — same reasoning
already applied to cc#1859's core-then-wiring split. Card NOT done — Fable verifies this push;
the frontend lands next.
