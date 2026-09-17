# cc#2140 — Image hosting: `image_assets` + `GET /api/images/{id}` + the article attachment

Founder direct, 16-Sep-2026: Claude/Fable can draw a chart image but had nowhere to host it.
This card is the hosting half only — it draws nothing. Two pushes: `d84c09d` (table, endpoint,
wiring) and `cc6ffcd` (the attachment row carries the setup it shows — see "Decision 2").

## What landed
- **`image_assets_endpoints.py`** (new, own router; `main.py` = import + `include_router` only):
  - `GET /api/images/{id}` — the bytes with the stored `Content-Type`; `Cache-Control: public,
    max-age=31536000, immutable` (an image is never updated in place — a new chart is a new row);
    `ETag: "img-<id>"` and `If-None-Match` → **304** without touching the bytes; `nosniff`;
    `Content-Disposition: inline`; an SVG is served with a script-less CSP; missing → 404 `no-store`;
    a row whose content type is not `image/*` → 415 (belt and braces over the CHECK).
  - `GET /api/images/{id}/meta` — everything but the bytes, plus the articles it is attached to.
  - `GET /api/images/status` — counts, latest id, total bytes, by purpose, the size cap.
  - `attachments(cur, news_ids)` — the ONE reader every news endpoint should use (cc#2139 wires it):
    `{news_id: {image_id, url, symbol, side, entry, target, sl, cmp_at_publish, setup_label, as_of, …}}`.
  - `ensure_schema()` — the same `CREATE … IF NOT EXISTS` DDL the tables were created with, run
    once per process from `/status`. **Never an ALTER.**
- **`image_assets`** (Postgres bytea, the card's design_recommendation): `id bigserial PK,
  content_type, image_bytes, purpose, width_px, height_px, created_at, created_by` + CHECKs:
  `content_type ~ '^image/'`, `octet_length(image_bytes) BETWEEN 1 AND 4 MB`, dims positive.
- **`polished_news_images`** — the attachment (see Decision 1 for why it is not a column):
  `news_id bigint PK → polished_news(id) ON DELETE CASCADE, image_id → image_assets(id) NOT NULL,
  symbol, side, entry, target, sl, cmp_at_publish, setup_label, as_of, attached_at, attached_by`
  + CHECKs: `side IN ('BUY','SELL')`, levels positive, levels need symbol+side, and **orientation**
  (`target < entry < sl` for SELL, `sl < entry < target` for BUY).

## Decision 1 — no ALTER on `polished_news` (logged in the room before building)
The card names a nullable `polished_news.chart_image_id` column. That is an `ALTER TABLE`, which
MAINTENANCE_LOCK_RULE (cc#351) holds for a weekend Railway-console run and the `run_sql` path
hard-blocks. cc#1519 precedent (the screeners `source` column): build the shape that works today.
`news_id` as the link table's PRIMARY KEY gives exactly one attachment per article — the semantics
of a nullable FK column — with FK integrity in both directions. When the column lands at a console
run, the reader becomes `COALESCE(p.chart_image_id, l.image_id)` inside `attachments()`; nothing
else moves. Fable: if you want the real column, say so on the card; the link table is the bridge
until then.

## Decision 2 — the setup levels live on the attachment row (logged before building)
cc#2139's strip needs symbol / side / entry / target / SL, and nothing structured exists:
`polished_news` has no jsonb, the levels are only text in `full_summary`, and the design ref's
numbers came from `tc_scanner_holds` (1065.2 × 0.97 / × 1.03 = 1033.24 / 1097.16; badge "SELL-MOM"
is that table's vocabulary). A symbol join there is fragile (several holds per symbol, the article
may state its own numbers, catalyst pieces have none). Chart and levels are drawn together at
publish time, so they are one row. **CMP and Unrealised % are never stored** — the page's server
computes them live through `cmp_resolver` (parity with every other CMP on the site);
`cmp_at_publish` is only the number the chart was drawn with. The table was 0 rows and minutes old
with nothing attached, so it was dropped and re-created with the new shape (my own empty table —
not an ALTER on a live one; `image_assets` untouched).

## How Claude attaches a chart (one statement, through run_sql)
```sql
WITH img AS (
  INSERT INTO image_assets (content_type, image_bytes, purpose, width_px, height_px, created_by)
  VALUES ('image/png', decode('<png hex>', 'hex'), 'stock_view_chart', 1200, 640, 'claude_chat')
  RETURNING id)
INSERT INTO polished_news_images
  (news_id, image_id, symbol, side, entry, target, sl, cmp_at_publish, setup_label, as_of, attached_by)
SELECT 5389, id, '360ONE', 'SELL', 1065.20, 1033.24, 1097.16, 1045.60, 'SELL-MOM', DATE '2026-09-16', 'claude_chat'
FROM img
ON CONFLICT (news_id) DO UPDATE
  SET image_id = EXCLUDED.image_id, symbol = EXCLUDED.symbol, side = EXCLUDED.side,
      entry = EXCLUDED.entry, target = EXCLUDED.target, sl = EXCLUDED.sl,
      cmp_at_publish = EXCLUDED.cmp_at_publish, setup_label = EXCLUDED.setup_label,
      as_of = EXCLUDED.as_of, attached_at = now(), attached_by = EXCLUDED.attached_by
RETURNING news_id, image_id;
```
A catalyst-only piece with a chart but no levels leaves `symbol … as_of` NULL: the page shows the
chart without the strip. The 16-Sep Stock Views are `polished_news` ids **5389** (360ONE), **5390**
(ASIANPAINT), **5391** (BANKINDIA); INDIANB is 5365. Theme: a raster chart is baked in one theme —
draw it **theme-neutral** (transparent background, mid-luminance lines) or attach the dark version
and accept it on light; flagged for cc#2139's report, not decided here.

## Reconciliation (the card's item 4)
Already settled by cc#2139's own spec (`reconciliation_with_cc2140`): Stock Views use the **static
snapshot** via this table; the live client-rendered chart is out of scope unless the founder asks.
Nothing left for me to pick; the frontend needs no per-article "which" flag.

## Verify
- `ast` / `py_compile` clean; `main.py` wiring only.
- **Local ASGI test** (FastAPI `TestClient`, fake DB, the real sample PNG bytes, GZip middleware
  in place as in production): `/status` JSON; `GET /api/images/1` → 200 `image/png`, bytes
  identical (sha256 equal), immutable cache header, ETag; `If-None-Match` → 304; missing → 404
  `no-store`; `0`/`-5` → 404; `abc` → 422; non-image row → 415; SVG → script-less CSP; `/meta`
  with the attachment row (numbers as floats, dates ISO); `attachments()` / `chart_image_ids()`.
- **Production DB round trip**: a stamped SAMPLE chart (480×260 PNG drawn with PIL, "not real
  data" written on it) inserted through `run_sql` → `image_assets.id = 1`, 2,723 bytes, sha256
  `268dedcd…ae1e` — identical to the local file. Constraints read back: 14 on `image_assets`,
  the 4 CHECKs + PK + 2 FKs on `polished_news_images`.
- **Orientation CHECK proven on a real article**: an INSERT for news 5389 with SELL levels the
  wrong way round (target 1097.16 > entry > sl 1033.24) was refused by
  `polished_news_images_levels_orientation`; no row written.
- **Live GET not exercised from this sandbox**: scorr.in is egress-blocked here (curl, Playwright
  and WebFetch alike), and the app records no build id in the DB. `perf_request_log` shows zero
  `/api/images` hits so far. **Arpit / Fable: open `https://scorr.in/api/images/1` — a stamped
  SAMPLE chart should render; `https://scorr.in/api/images/status` should read `images: 1`.**
  The sample row is the first-run evidence; delete it once a real chart exists, or keep it as id 1.

## Not touched
`polished_news` (columns, triggers, the body guard cc#870, the AI Editorial data gate cc#1729),
every news endpoint and page (cc#2139 wires the reader), no upload API (by design), no S3/R2.
