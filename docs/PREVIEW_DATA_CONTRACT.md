# PREVIEW DATA CONTRACT — cc#868

Read-only audit. Every displayed value on every screen in `previews/`, mapped to an endpoint +
field or a table + column. **Nothing here is wired.** This document is the input to the
per-screen PROMOTION cards (session_log 16230, `PREVIEW_PROMOTION_DECISION_V1`): approved screens
are promoted to real routes with real data; real data is never wired into `previews/*.html`.

Generated 05-Aug-2026 from the folder listing, not a typed list.

## Legend

| Status | Meaning |
|---|---|
| **SOURCED** | An endpoint + field, or a table + column, exists today and carries this value. |
| **UNSOURCED** | No endpoint or column exists. Named honestly; **no endpoint was invented**. |
| **SOURCED_BUT_EMPTY** | A real source exists and currently returns **zero rows**. Different from unsourced — the wiring will work, the screen will render empty. |
| **BLOCKED_SOURCE** | The only apparent source is a table known to be bad. Must not feed the value. |

---

## Screens found (7)

Enumerated from `previews/`, per task item 1. Count matches the card's evidence block.

`home` · `intel` · `models` · `models_tools` · `v8` · `v8_positions` · `v8_surfaces`

---

## 1 · home.html

Renders **two** SmartGain hero states (flat and loaded) as a design demo, so some values appear twice.

| Element | Dummy value | Source | Status |
|---|---|---|---|
| Index tile — name | `Nifty 50`, `Bank Nifty` | `GET /api/v8/domestic_live` | SOURCED |
| Index tile — day % | `+0.04%`, `−0.29%` | `GET /api/v8/domestic_live` | SOURCED |
| Index strip session chip | `CLOSED 05 AUG` | derived from IST clock + `nse_holidays.is_trading_day` | SOURCED |
| Hero label | `Week M2M · MHK40` | static label, account from `smartgain_holdings.account` | SOURCED |
| Hero badge | `FLAT` / `LIVE` | derived: count of `smartgain_holdings` rows | SOURCED_BUT_EMPTY |
| Hero net | `+₹200.38` / `−₹7,186.50` | `GET /api/smartgain/m2m` | SOURCED_BUT_EMPTY |
| Realised | `+₹407` / `₹0` | `GET /api/smartgain/m2m` | SOURCED_BUT_EMPTY |
| Unrealised | `₹0` / `−₹7,187` | `smartgain_holdings.mtm` (SUM) | SOURCED_BUT_EMPTY |
| Brokerage | `−₹206.62` / `−₹84.19` | `GET /api/smartgain/m2m` | SOURCED_BUT_EMPTY |
| Long value / Short value | `₹0` / `₹7,64,942` | `smartgain_holdings.qty × ltp` grouped by `direction` | SOURCED_BUT_EMPTY |
| Flat message | `No positions open. Six trades closed this week.` | derived: holdings count + closed-trade count | SOURCED_BUT_EMPTY |
| Closed-trade count | `6 closed` | `personal_journal` (week window) | SOURCED |
| Position row — symbol | `NIFTY`, `APOLLOHOSP` | `smartgain_holdings.symbol` | SOURCED_BUT_EMPTY |
| Position row — side | `SHORT` | `smartgain_holdings.direction` | SOURCED_BUT_EMPTY |
| Position row — source tag | `Sell Momentum`, `Undefined` | `smartgain_holdings.source_tag` | SOURCED_BUT_EMPTY |
| Position row — MTM | `−₹347` | `smartgain_holdings.mtm` | SOURCED_BUT_EMPTY |
| Position row — qty | `10 qty` | `smartgain_holdings.qty` | SOURCED_BUT_EMPTY |
| Position row — value | `₹2,46,247` | `smartgain_holdings.qty × ltp` | SOURCED_BUT_EMPTY |
| Position row — day % | `−0.14%` | `v8_metrics.day_1d` by symbol | SOURCED |
| Position row — weight | `Wt 32.2%` | derived: row value ÷ book value | SOURCED_BUT_EMPTY |
| Position row — avg / CMP | `Avg ₹24,590.00 · CMP ₹24,624.65` | `smartgain_holdings.entry_price`, `.ltp` | SOURCED_BUT_EMPTY |
| Daily M2M bar strip (Mon–Fri) | bar heights | `GET /api/smartgain/daily_m2m` | SOURCED |
| Model summary cards | counts per model | `GET /api/models/status` (cc#860) | SOURCED |
| `View Holdings ›` CTA | link | route `/holdings` | SOURCED |

**Screen total 24** — SOURCED 6, SOURCED_BUT_EMPTY 18, UNSOURCED 0, BLOCKED_SOURCE 0.

> **`smartgain_holdings` currently has ZERO rows.** Verified 05-Aug. Every hero and position value
> above has a real, correct source that returns nothing today. The wiring will work; the screen will
> render its flat state. This is *not* a blocker — but shipping it without knowing that would look
> like a broken page.

---

## 2 · intel.html

| Element | Dummy value | Source | Status |
|---|---|---|---|
| Header count | `232 items` | `COUNT(*) polished_news` (day window) | SOURCED |
| Newest headline time | `newest headline 07:36 IST` | `MAX(polished_news.published_time)` | SOURCED |
| Section headers | `TODAY'S EDITORIAL` | `polished_news.category` | SOURCED |
| Item — headline | `Foreign selling in Indian shares…` | `polished_news.headline_clean` | SOURCED |
| Item — body / summary | fold text | `polished_news.summary`, `.full_summary` | SOURCED |
| Item — time | `07:23` | `polished_news.published_time` | SOURCED |
| Item — source line | `AI Editorial · 05-Aug 07:23 IST` | `polished_news.source` + `.published_time` | SOURCED |
| Item — sentiment tag | `BULLISH`, `CAUTIOUS` | `polished_news.sentiment` | SOURCED |
| Item — category tag | `DOMESTIC` | `polished_news.category` | SOURCED |
| Item — impact tag | `HIGH`, `MEDIUM` | `polished_news.impact` | SOURCED |
| Item — symbol chip | `NIFTY` | `polished_news.mentioned_symbols` | SOURCED |
| Cover line | `showing 8 of 232` | derived: page size + total count | SOURCED |

**Screen total 12** — SOURCED 12, everything else 0.

> Cleanest screen in the set. `GET /api/news/polished` already serves this shape;
> `polished_news` had **172 rows today**, so it renders populated on day one.

---

## 3 · models.html

| Element | Dummy value | Source | Status |
|---|---|---|---|
| Header count | `4 registered` | `GET /api/models/status` → `count` | SOURCED |
| Header session state | `market closed` | `/api/models/status` → `in_session` | SOURCED |
| Model name | `V8` | → `models[].display_name` | SOURCED |
| Model description | `Live 5-min signals across the futures universe` | → `models[].description` | SOURCED |
| Model footer | `Last run 15:15 IST · ok` | → `models[].last_run_human`, `.state_reason` | SOURCED |
| State rail / badge | LIVE / STALE / OFF | → `models[].state` | SOURCED |
| Card tap route | `/dashboard` | → `models[].route` | SOURCED |

**Screen total 7** — SOURCED 7.

> **Fully sourced by cc#860, which shipped today.** This screen can be promoted immediately with no
> new backend work — every value maps to one field of `/api/models/status`, and the badge already
> follows run data per ENGINE_LIVENESS_RULE 13829.

---

## 4 · models_tools.html

| Element | Dummy value | Source | Status |
|---|---|---|---|
| TC — symbol / CMP / time | `SYMBOL`, `CMP 1,742 · 15:30` | `GET /api/trade-check/v4` | SOURCED |
| TC — verdict | `TAKE IT` | `/api/trade-check/v4` → `final_verdict` | SOURCED |
| TC — score `/20` | `/20` | `/api/trade-check/v4` → score fields | SOURCED |
| TC — conviction line | `High conviction` | `/api/trade-check/v4` | SOURCED |
| TC — rule rows | `R17 Valuation — above peer median`, `PE 38` | `GET /api/trade-check/v4/detail` | SOURCED |
| TC — `Show 16 passing rules` | count | derived from `/v4/detail` rule array | SOURCED |
| TC — direction toggle | `LONG` / `SHORT` | request param, not stored | SOURCED |
| Index Intel — name | `NIFTY 50` | `GET /api/v8/domestic_live` | SOURCED |
| Index Intel — live value | `24,624.65` | `/api/v8/domestic_live` | SOURCED |
| Index Intel — bias | `LONG` / `SHORT` | `GET /api/v8/market_mood` | SOURCED |
| Index Intel — duration | `since 31 Jul · 4 sessions` | **no stored bias-since date** | **UNSOURCED** |
| Index Intel — SUPERTREND | `1.8% away` | **no supertrend column or endpoint** | **UNSOURCED** |
| Index Intel — other indicator keys | `1.2% away` | same as above | **UNSOURCED** |

**Screen total 13** — SOURCED 10, UNSOURCED 3.

> The three UNSOURCED rows are the real finding on this screen. `SUPERTREND` and the "% away"
> indicator readouts have **no column and no endpoint anywhere in the repo** — grep returns nothing.
> The bias duration ("since 31 Jul · 4 sessions") needs a persisted bias-change date that is not
> stored either. Per the card's stop condition these are listed and left; **no endpoint was invented
> and none should be built inside a promotion card without its own spec.**

---

## 5 · v8.html

Eleven tab-surface cards, matching the post-cc#850 `TAB_ORDER` (eleven, not twelve).

| Element | Dummy value | Source | Status |
|---|---|---|---|
| Header | `Long-short futures · 209 symbols` | `COUNT(*) futures_universe WHERE is_active` | SOURCED |
| Gate chip | `GATE --` | `GET /api/v8/market_mood` | SOURCED |
| Tier label | `Primary` | static layout property, not data | SOURCED |
| Card — surface name | `Master Dashboard` … | `TAB_LABELS` (v8_dashboard.html) | SOURCED |
| Card — count (baskets) | `21`, `9`, `1` | `GET /api/v8/qualified/{basket}` | SOURCED |
| Card — count (trade log) | `85` | `GET /api/v8/trades` | SOURCED |
| Card — sub-line | `open · P&L --` | `GET /api/v8/positions` + `v8_paper_trades.pnl` | SOURCED |
| Card — `LIVE` chip | `LIVE` | `scheduler_master.last_run_at/last_status` via `/api/models/status` | SOURCED |
| Card — `STALE 31h` chip | `STALE 31h` | same, with age | SOURCED |
| Card — `REFERENCE` chip | `REFERENCE` | static: surface is reference-only, no run data | SOURCED |
| Card — `--` placeholder | `--` | absent value, per the `--` never `0.0` rule | SOURCED |

**Screen total 11** — SOURCED 11.

> **No card here may read `v8_filter_state`** — see the blocked-source note below. Basket counts must
> come from `v8_qualified` / `/api/v8/qualified/{basket}`, which is live and correct
> (**19 qualified rows today**).

---

## 6 · v8_positions.html

| Element | Dummy value | Source | Status |
|---|---|---|---|
| Header count | `20` | `COUNT(*) v8_paper_positions WHERE status='OPEN'` | SOURCED |
| Header net | `Net +Rs.6,043` | derived from open positions × live CMP | SOURCED |
| Header sub | `Net of Rs.500 per trade` | static cost assumption | SOURCED |
| Row — symbol | `OFSS` | `v8_paper_positions.symbol` | SOURCED |
| Row — side | `LONG` / `SHORT` | `v8_paper_positions.side` | SOURCED |
| Row — basket | `Buy Reversal` | `v8_paper_positions.basket` | SOURCED |
| Row — age | `D1 · 05 Aug` | `v8_paper_positions.entry_ts` | SOURCED |
| Row — `NEW` flag | `NEW` | derived: `entry_ts::date = today` | SOURCED |
| Row — P&L | `+Rs.17,900` | derived: `(cmp − entry_price) × qty × side` | SOURCED |
| Row — P&L % | `+1.21%` | derived, same inputs | SOURCED |
| Row — entry / CMP | `11,740`, `11,556` | `.entry_price`, live CMP resolver | SOURCED |
| Row — STOP | `11,209` | `v8_paper_positions.stop_loss` | SOURCED |
| Row — TARGET | `11,903` | `v8_paper_positions.target` | SOURCED |
| Row — pivot star ★ | `★` blue / red | `v8_pivot_star_log.star_color` | **SOURCED_BUT_EMPTY** |

**Screen total 14** — SOURCED 13, SOURCED_BUT_EMPTY 1.

> **20 open positions today**, so this screen renders fully populated — except the star.

---

## 7 · v8_surfaces.html

| Element | Dummy value | Source | Status |
|---|---|---|---|
| Mood gate verdict | `BULLISH` | `GET /api/v8/market_mood` | SOURCED |
| Mood gate tag | `GATE OPEN` | `/api/v8/market_mood` | SOURCED |
| Mood as-of | `15:30` | `/api/v8/market_mood` | SOURCED |
| ADR | `1.24` | `GET /api/v8/adr_intraday` → `adr_intraday.adr` | SOURCED |
| Advances / declines | counts | `adr_intraday.advances`, `.declines` | SOURCED |
| PCR | value | `GET /api/daily/pcr` → `pcr_daily.pcr` | SOURCED |
| Pass counts | `14/20`, `6/10` | `GET /api/v8/stock_passcount/{basket}` | SOURCED |
| Symbol card — GVM | `8.4` | `gvm_scores.gvm_score` | SOURCED |
| Symbol card — CMP | `CMP 1,742` | live CMP resolver | SOURCED |
| Symbol card — 1D | `1D +1.2%` | `v8_metrics.day_1d` | SOURCED |
| Symbol card — DMA50 | `DMA50 1,690` | `v8_metrics.dma_50` | SOURCED |
| Symbol card — pivots | `S1 1,684`, `PP 1,712`, `R1 1,769` | `v8_paper_pivots.s1/.pp/.r1` | SOURCED |
| Symbol card — status chip | `OPEN`, `SIGNAL` | `v8_paper_positions.status`, `v8_qualified` | SOURCED |
| Symbol card — star ★ | `★` | `v8_pivot_star_log` | **SOURCED_BUT_EMPTY** |
| Symbol card — time ago | `09:45` | `v8_qualified.signal_ts` | SOURCED |

**Screen total 15** — SOURCED 14, SOURCED_BUT_EMPTY 1.

---

## Item 4 · Flagged sources

### BLOCKED_SOURCE — `v8_filter_state`

Confirmed 05-Aug: **all six baskets read `enabled = false`**, with `disabled_reason` values like
*"starvation: 0 signals in last 5 trading days"* and *"WR decay: rolling 50.0% over 10 closed vs
baseline 77.4%"* — while `v8_qualified` wrote **19 rows today**. The table describes a state the
engine is not in.

**No state chip on any screen may read it.** Every basket state chip must derive from
`v8_qualified` (did it produce signals) or `scheduler_master` (did its job run), never from this
table. Counted once as BLOCKED_SOURCE rather than per-screen, because it is one prohibition.

### SOURCED_BUT_EMPTY — `v8_pivot_star_log`

**Zero rows**, verified. The pivot star on `v8.html`, `v8_positions.html` and `v8_surfaces.html` has
a real, correct source (cc#856 shipped it, and `bg_pivot_star` runs ok every 5 min) that has simply
not fired yet. 05-Aug was a legitimate zero-star day. The wiring is correct; the marker renders on
the first day the rule fires.

### SOURCED_BUT_EMPTY — `smartgain_holdings`

**Zero rows**, verified. Drives the entire `home.html` hero and its position list. Same class: the
source is right, the book is flat.

---

## Item 5 · State rail / badge audit

Four states told apart by shape: LIVE (solid, breathing) · STALE (long amber dashes) ·
CLOSED (short grey dots) · REFERENCE (short blue inset).

| Rail instance | Deciding field | Verdict |
|---|---|---|
| `models.html` model cards | `/api/models/status` → `models[].state` | **OK** — derives from `scheduler_master.last_run_at` + `last_status`, per 13829. |
| `v8.html` basket cards | must be `v8_qualified` (signals today) | **OK if wired to `v8_qualified`.** Reading `v8_filter_state` instead would show all six dead while four produce signals. |
| `v8.html` `REFERENCE` chip | none — static surface property | **OK.** Reference surfaces have no run data by definition; the chip says exactly that rather than implying liveness. |
| `home.html` hero badge | `smartgain_holdings` row count | **OK** — FLAT vs LIVE is a book-state fact, not a liveness claim. |
| `v8_surfaces.html` mood chip | `/api/v8/market_mood` | **OK.** |
| `intel.html` impact/sentiment tags | `polished_news.impact`, `.sentiment` | **OK** — these are content tags, not state rails. |

**No rail was found whose state has no data source behind it.** The one real risk is a rail wired to
`v8_filter_state`, which is why it is called out as BLOCKED_SOURCE above.

### The after-15:30 rule — can it be evaluated server-side?

**Yes.** After the close every rail must read CLOSED, never STALE. All three inputs are
server-available:

* session boundaries — `worker/fyers_feed.py` constants `EQ_CONTINUOUS_END 15:15`,
  `EQ_AUCTION_END 15:35`, `FUT_CLOSE 15:40` (cc#855)
* trading-day test — `nse_holidays.is_trading_day`
* the same session logic already exists in `model_launcher._in_session()` and
  `scheduler._is_cash_continuous()`

`model_launcher.badge_for()` already implements exactly this for market-gated jobs: out of hours the
time spent waiting for the next open does not count against a job, so a 5-minute market job reads
LIVE at 21:00 rather than STALE. The promotion cards should reuse that function rather than
re-deriving it — a second implementation is how two surfaces start disagreeing.

---

## Item 6 · Reachability audit (code only)

| Check | Result |
|---|---|
| (a) `/preview` + `/preview/{name}` routes and allowlist | **Confirmed.** `preview_endpoints.py` — allowlist `^[a-z0-9_-]+$`, rejected **before** any filesystem access, path built from the validated name, plus a realpath containment check. |
| (b) Prefix auth gate covers `/preview/`, not just the literal | **Confirmed.** `main.py:315-316` — `_is_preview = path == '/preview' or path.startswith('/preview/')`, then `if (path in PROTECTED or _is_preview) and not _is_authed(request)` → `/login`. The literal-only form would have left every screen ungated. |
| (c) NAV entry + `NAV_REGISTRY` mirror | **Confirmed.** `['/preview', '◱', 'Previews']` last in the NAV array (`pwa_endpoints.py`), mirrored in `NAV_REGISTRY` (`main.py`) as `("Previews — review screens, temporary", "nav")`. |
| (d) Every response carries `no-store` | **Confirmed.** `_NO_STORE` applied to all four return paths in `preview_endpoints.py` — index, 404-bad-name, 404-missing-file, and the 200. |

### Can an installed PWA client be served a STALE preview by the service worker?

**NO.** Confirmed, with line numbers in `pwa_endpoints.py`:

1. **`/preview` is not in `SHELL`.** Line 100–101: `SHELL = ['/', '/pwa.js', '/static/manifest.json',
   '/static/icon-192.png', '/static/icon-512.png']`. So the cache-first branch at **line 148**
   (`if (SHELL.includes(url.pathname))`) can never match a preview URL.
2. **Opening a screen is a navigation, and navigations are network-first.** Line 123–124:
   `if (req.mode === 'navigate') { e.respondWith(fetch(req).catch(() => caches.match('/'))); return; }`
3. **That branch never writes to the cache.** It has no `cache.put()` — compare the `/pwa.js` branch
   at line 135–143, which does. So a preview response is never stored by the SW at all.
4. **The offline fallback is the app shell, not a stale screen** — `caches.match('/')`. Worst case
   offline you get Home, never yesterday's preview.

This **confirms** cc#867's conclusion for the `/preview` path, and for a stronger reason than the
BUILD_ID rotation it cited: the path is never cached in the first place, so cache-name rotation is
not even load-bearing here. Combined with `no-store` at the HTTP layer, a preview cannot be stale.

**So a founder-visible problem on these pages is not caching.** Per the card's own evidence the
backend is healthy, which leaves auth gating (confirmed correct above) and unsourced values — the
three on `models_tools.html`.

---

## Summary counts

| Status | Count |
|---|---|
| **Total values audited** | **97** |
| SOURCED | 73 |
| SOURCED_BUT_EMPTY | 20 |
| UNSOURCED | 3 |
| BLOCKED_SOURCE | 1 |

73 + 20 + 3 + 1 = **97** ✓

### Per screen

| Screen | Total | SOURCED | SOURCED_BUT_EMPTY | UNSOURCED |
|---|---|---|---|---|
| home | 24 | 6 | 18 | 0 |
| intel | 12 | 12 | 0 | 0 |
| models | 7 | 7 | 0 | 0 |
| models_tools | 13 | 10 | 0 | 3 |
| v8 | 11 | 11 | 0 | 0 |
| v8_positions | 14 | 13 | 1 | 0 |
| v8_surfaces | 15 | 14 | 1 | 0 |
| **BLOCKED_SOURCE** (`v8_filter_state`, counted once) | 1 | — | — | — |

---

## The three UNSOURCED rows, for the follow-up card

All on `models_tools.html`, all in the Index Intel block:

1. **`SUPERTREND` — "1.8% away".** No supertrend column and no endpoint exists. Grep across the repo
   returns nothing.
2. **Second indicator readout — "1.2% away".** Same.
3. **Bias duration — "since 31 Jul · 4 sessions".** Requires a persisted bias-change date.
   `/api/v8/market_mood` returns the current bias but nothing records when it last flipped.

Per the stop condition these were listed, not built. Each needs its own spec before a promotion card
touches that screen — an indicator is a calculation decision, not a wiring detail.

## Recommended promotion order

1. **`models`** — 7/7 sourced by cc#860, zero new backend. Promote first.
2. **`intel`** — 12/12 sourced, 172 rows today, renders populated immediately.
3. **`v8_positions`** — 13/14, 20 open positions today; only the star is empty.
4. **`v8_surfaces`** and **`v8`** — fully sourced, but `v8` must be wired to `v8_qualified`, never
   `v8_filter_state`.
5. **`home`** — correct sources, but flat until `smartgain_holdings` has rows.
6. **`models_tools`** — last. Needs the three UNSOURCED decisions resolved first.
