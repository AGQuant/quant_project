# cc#2139 — Stock Views articles get a chart card (web `/intel` + app `/m/intel`)

Gate: cc#2140 landed first (`cc6ffcd`), so the display half was built against the real
`image_assets` + `polished_news_images` tables. Display side only — this task draws nothing for
production; the one chart it did draw was for the end-to-end proof (see Evidence).

## Read-only findings (task 2) — confirmed before adding anything
- **Web**: `/intel` → `scorr_news.html`; rows from `GET /api/news/polished`
  (`news_endpoints.news_polished`). A Stock Views row (`category = 'Stock Views'`, title case) renders
  through `artFull()` (the AI-Editorial card); Domestic/Global/IPO through `artCompact()`. **No chart
  or image slot existed for any category** — the only way an image could appear was a markdown
  `![]()` inside the body through `marked`, unstyled.
- **App**: `/m/intel` → `mobile/intel.html` (`mobile_endpoints.m_intel`); rows from
  `GET /api/mobile/intel` (`shape()`), Stock Views fall into the plain `feed` card; the body is
  `esc()`-escaped and split into `<p>`, so an image has to be real DOM, never body text. No slot.
  The template has no `<style>` block — its CSS lives in `MOBILE_CSS` (`mobile_endpoints.py`).
- The two pages share **no** renderer, endpoint or payload key names.
- **Levels**: entry / target / SL / side exist nowhere structured — only as text in `full_summary`.
  The design ref's numbers came from `tc_scanner_holds` (360ONE SELL-MOM: entry 1,065.20, target
  1,033.24, SL 1,097.16 — verified in that table). Hence cc#2140's decision 2: the attachment row
  carries the setup, and this card renders from it.

## What landed
- **`stock_view_chart.py`** (new): `overlay(cur, rows, id_key)` → `row["chart"]` = the attachment
  dict + live `cmp` / `cmp_live` / `cmp_source` via `cmp_resolver.resolve_cmp_many` (the same CMP
  every other surface shows, never stored) + `unrealised_pct` from **one formula**
  (`(entry − cmp)/entry` for SELL, `(cmp − entry)/entry` for BUY) + `has_levels`. A CMP failure
  degrades to `--` cells; a DB failure is caught by the caller with a rollback so the news page
  never blanks. `image_assets_endpoints.attachments()` now joins `image_assets` for
  `content_type / width_px / height_px` (the `<img>` gets real dimensions, no layout jump).
- **`news_endpoints.py`** (`/api/news/polished`, both the listing and the search branch) and
  **`mobile_endpoints.py`** (`/api/mobile/intel`): call the overlay last on the connection, own
  `try`/`rollback`; every article now carries `chart` (dict or `null`). `shape()` passes it through.
- **Web card** (`scorr_news.html` `svChart()`, rendered by `artFull()` between the metarow and the
  standfirst, **only when `category === 'Stock Views'` and `chart.url` exists**): symbol, a
  `SHORT · <label>` / `LONG · <label>` badge (from `side`, never hardcoded), "as of <date>", the
  `<img src="/api/images/<id>">` (lazy, async), and the 5-cell strip **Entry / Target / Stop Loss /
  CMP / Unrealised** — the CMP label reads `CMP · live` or `CMP · last close` from `cmp_live`, so a
  close is never dressed as live. No levels → chart alone, no strip. No attachment → nothing extra
  (INDIANB-type pieces unchanged). Web tokens only; strip folds 5 → 3 + 2 below 560 px.
- **App card** (`mobile/intel.html` `svChartHtml()`, between the headline and the summary, same
  gates), CSS in `MOBILE_CSS` with r5 tokens; the strip is 3 + 2 at phone width. Every class is
  `svc-` prefixed including the cell label/value — the first cut used `.l`/`.v` and the app's own
  `.v` pill rule dressed the values in gold-railed boxes (the cc#964/967 collision class), caught in
  the screenshot and renamed on both surfaces.

## Defaults the spec listed, as built (flag for Arpit to correct on review)
- Area/line style, no volume, reward/risk zones across the full chart width — all in the chart
  image, which Claude/Fable draws; the one CC drew for the proof follows exactly these defaults.
- SHORT: reward zone below entry, risk above; LONG inverted — the card's badge, the Unrealised
  sign and the strip come from `side`, and the harness proves the BUY case (`LONG · BUY-REV`,
  `+3.00%` at CMP 103 vs entry 100) beside the real SELL case.
- **Theme (task 5)**: draw the image **theme-neutral** — transparent background, mid-tone
  colours (blue line, amber/green/red levels, grey text). That is what the proof chart is, and it
  reads on the dark web panel, the white light panel and the app's dark tile (screenshots looked
  at). A raster baked dark would still sit legibly on the white card as a dark tile, but one
  neutral image is simpler than two ids. Flagged, not decided for Fable.
- The card renders in the feed list on the app (not only inside the expanded body): the chart is
  the point of the piece, so it is visible without a tap. Easy to move under "Read ›" if preferred.

## Evidence
- `ast`/`py_compile` clean on the 4 Python files; `node --check` clean on both pages' inline
  blocks; 0 new literal fallbacks (tokens only).
- **Unit test** of the overlay with a fake reader + mocked CMP: real SELL row → `cmp 1045.6`,
  `unrealised +1.84`, `has_levels`; chart-only row → no levels, no CMP; CMP failure degrades.
- **Playwright on the real pages** (real `scorr_news.html` with the real theme boot + web tokens;
  real `mobile/intel.html` with r5 + the real `MOBILE_CSS`), payloads built from the real rows 5389
  / 5390 / 5365 plus two harness-only rows (a BUY Stock View; a Domestic row WITH an attachment):
  5 surfaces (web dark/light 1280 px, web dark 375 px, app goldnight/aquawhite 390 px). Each:
  exactly **2** chart cards (5389 + BUY) — the Domestic row with an attachment renders none
  (do_not_touch proven), 5390/5365 none; image loaded 1000×420 and shown 858 px wide on web / 336
  on the app; strip 5 columns on web, 3 on web-375 and app; every cell value exact
  (`1,065.20 / 1,033.24 / 1,097.16 / CMP · last close 1,045.60 / +1.84%`); card sits between
  `.art-metarow` and `.art-stand` (web) / `.hl` and `.sum` (app); no horizontal overflow; worst
  text contrast vs the card 5.73:1 dark, 5.21:1 app, **3.41:1 on web light** = the contract's own
  `--grn` #0E9F62 on white (the same token every P&L green uses; ratchet-exempt class), the rest
  ≥ 3.6. Screenshots looked at (VISUAL_VERIFY_GATE_V1).
- **Production, end to end**: `polished_news` **5389** (360 ONE, 16-Sep) now has attachment
  `image_id 2` — an SVG drawn by CC from the real last 45 closes in `raw_prices` (as of
  16-Sep-2026, CMP 1,045.60) with the real `tc_scanner_holds` SELL-MOM setup — written with the
  one-statement recipe from cc#2140 (`WITH img AS (INSERT …) INSERT … ON CONFLICT DO UPDATE`), so
  the recipe itself is proven. Logged in the room before it was written; Fable's own chart replaces
  it with the same statement. 5390 (ASIANPAINT) and 5391 (BANKINDIA) deliberately left without.
- **Live check ask** (this sandbox cannot reach scorr.in): open `/intel` and `/m/intel` — the
  360 ONE piece shows the chart card with the strip; Asian Paints and Bank of India show none.

## Not touched
`polished_news` write path / body guard / AI Editorial gate; Domestic/Global/IPO/AI Editorial
rendering; the Stock Views text format; `scorr_chart_card.js` (no live client chart — out of
scope by the spec's reconciliation); `scorr_news_row.js` (used only by `/m/digest`).
