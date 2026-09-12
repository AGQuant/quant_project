# cc#2024 — Alerts IA rebuild: two-level tabs, no Waiting rail, no duplicated prose

Founder direction + screenshot, 12-Sep-2026 22:29 IST, `/m/alerts`. The flat six-chip filter row
(`All 9 / Live 2 / Waiting 5 / Closed 2 / Long 7`) made Live and Long mutually exclusive — a reader
could not be on both at once, which was the root of the confusion. Founder ruling: two-level tabs
(major: Live/Closed; then a second selection inside them), three named categories (Futures Long,
Futures Short, Equities), only approved trades, and — a later, explicit revision superseding an
earlier instruction to keep signals-only Waiting — **no Waiting cards at all**, since `/m/alerts`
is client-facing.

## Item 1/2 — two independent filter dimensions

`IA.filter` (one mutually-exclusive string) is replaced by `IA.tab` (`live`/`closed`, level 1) and
`IA.cat` (`all`/`fut_long`/`fut_short`/`eq`, level 2, scoped inside the selected tab).
`matches(c)` now tests both: `c.status !== IA.tab` excludes by tab first (and, by construction,
excludes `'waiting'` entirely — `IA.tab` is never anything else), then `catOf(c) === IA.cat`
(skipped when `IA.cat === 'all'`). `catOf()`:

```
FUT + BUY  -> fut_long
FUT + SELL -> fut_short
EQ (any direction) -> eq        // cash equity is effectively long-only here
anything else (OPT, …) -> null  // matches no category chip -- item 3
```

Two new chip rows, `#ia-tabs` (level 1) and `#ia-cats` (level 2, replacing the single
`#ia-filters`), both reusing the existing `.ia-filters`/`.ia-chip` look — no new CSS. Counts:
level-1 tab counts reuse the server's own `counts.live`/`counts.closed` (already correct); level-2
category counts are computed client-side from the already-fetched `ideas` array, scoped to
whichever tab is currently selected (`catCounts()`) — the server's `counts` block has no
`fut_long`/`fut_short`/`eq` buckets, so no second endpoint call was added, per the card's own
fallback instruction. Switching the level-1 tab resets the category chip to All (a UI judgment call
within scope — a category selection from one tab context carrying silently into a different tab
would be confusing, not requested against, and not mentioned by the card either way).

## Item 3 — OPT rows, counted against the real database, not guessed

`instrument === 'OPT'` requires `source_engine = 'Index Intel'` on an **approved** `trade_alerts`
row whose `v10_trades.leg = 'OPT'` (read from `_resolve_origin`/`_origin_v10` in
`trade_alerts_endpoints.py` — a `leg` other than `'OPT'` is the hidden V10-futures-leg case, 36703,
excluded from the ideas list before it would ever reach a category check). Queried the real
production database directly:

```sql
SELECT count(*) AS approved_index_intel_total,
       count(*) FILTER (WHERE v.leg = 'OPT') AS opt_visible,
       count(*) FILTER (WHERE v.leg IS DISTINCT FROM 'OPT') AS hidden_futures_leg
FROM trade_alerts a
LEFT JOIN v10_trades v
  ON v.symbol = split_part(a.source_ref, '@', 1)
 AND to_char(v.entry_ts AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS') = split_part(a.source_ref, '@', 2)
WHERE a.status = 'approved' AND a.source_engine = 'Index Intel';
-- result: approved_index_intel_total = 0, opt_visible = 0, hidden_futures_leg = 0
```

**Zero OPT-instrument rows exist among approved alerts today.** Per the card's own instruction
("if the count is non-zero, STOP and ask... do not invent a fourth chip") there is nothing to stop
for right now — the interim behaviour (an OPT row would appear under All, under no category chip)
is implemented in `catOf()`/`matches()` and will apply correctly the moment one exists, without
further code changes, but does not need a founder ruling today.

(Full `trade_alerts` status breakdown at query time: `approved=5, pending=4, triggered=0` — 9 rows
total, matching the founder's screenshot's "All 9" order of magnitude; the exact Waiting count
differs slightly from the screenshot's "5" — this container queried several hours later than the
22:29 IST screenshot, and an alert can be created, triggered, or dismissed in that window; not
investigated further as it is expected drift, not a discrepancy in this card's own logic.)

## Item 4 — the Waiting rail, removed entirely

Deleted: the `waiting` filter option (gone with the whole old `FILTERS` array), the
`"Waiting for price"` `.ia-sect` section header and the `main`/`wait` split in `renderList()`,
`waitBody()`, the `.ia-dist` distance track, the `.ia-acts` Approve/Dismiss buttons, `act()`,
`cardErr()`, the `#ia-list [data-act]` click listener, and the waiting branches inside `cardHead()`
(now unconditional — a card reaching `cardHead()`/`cardBody()` is always live or closed, since
`matches()` excludes `'waiting'` by construction before a card ever gets built). The now-dead CSS
(`.ia-dist`, `.ia-acts` + variants, `.ia-cerr`, `.ia-sect` + variants) removed alongside.

**Verified consequence (read, not assumed), matching the card's own note:** `/api/alerts/approve`
and `/api/alerts/dismiss` are untouched (do-not-touch) and still correct, but after this card they
are reachable from **no screen** — `waitBody()` was their only caller anywhere in this repo for the
Approve/Dismiss UI, and `/m/myalerts` (`trade_alerts_app.py`, cc#1896) is read-only: its own
docstring names `/m/alerts` as "the fuller operational surface", and its `/api/mobile/myalerts`
payload carries `symbol`/`direction`/`trigger_price`/`created_at` with no action affordance. The
cc#1586 founder pre-trigger override therefore has no entry point after this push. **Relocating
those controls (most naturally onto `/m/myalerts`, which already lists exactly these pending
alerts) is a founder decision, filed as a follow-up — not built into this card, per its own explicit
instruction.**

## Item 5 — approved-only, verified against the endpoint's own branching

Read `/api/alerts/ideas` (`trade_alerts_endpoints.py`) end to end: the query pulls
`status IN ('approved','pending','triggered')`, then branches
`if row["status"] in ("pending","triggered"): card["status"] = "waiting"` — **unconditionally, no
exception** — and only the `else` path (i.e. `row["status"] == 'approved'`, the only remaining
possibility) sets `card["status"] = "closed" if closed else "live"`. **There is no code path where
a `pending` or `triggered` DB row can become a `live` or `closed` UI card** — the branch is
exhaustive and unconditional, not merely usually-true. No client-side `approved_at_ist` defensive
check was needed on top of this (the card's own fallback, for if such a path existed) — cross-
checked anyway against the real table: all 5 `approved` rows have a non-null `approved_at`.

## Item 6/7 — the duplicated blurbs gone, the as-of stamp survives, bare

Removed: `.ia-hon` (the honesty line under the stats strip), the `#ia-foot` prose sentence ("Ideas
are the approved book...") — the two said the same thing twice, top and bottom of one screen — and
`why()`/`.ia-why` on the card. `#ia-foot` still renders `"As of HH:MM IST"` plus the last-close
qualifier when `price_basis === 'CLOSE'`, with the prose gone; the `load()`-failure path
(`"Refresh failed · showing …"`) is untouched.

## Do-not-touch, confirmed

`/api/alerts/approve`, `/dismiss`, `/create`, `/ideas` — zero changes, this card is one template.
The cc#1507 create-alert flow (`alOpen`/`alSearch`/`alSym`/`alPick`/`alSubmit`, `al-*` CSS, the
`#new` entry point) — untouched, still reachable (unaffected code, not re-tested). `/m/myalerts`
and `trade_alerts_app.py` — not touched. The stats strip, sparkline, plan tiles, stop→target track
and `foot()` — same numbers, same markup, only the prose line above them is gone. The cc#1634 bell
deep-link (`#ia-<id>` scroll-into-view) — code untouched, and re-verified: a fresh arrival at
`#ia-2` (a live idea) lands and scrolls correctly.

## Verify — real Chromium, the real page, no reimplementation

`node --check` clean on both inline `<script>` blocks. `mobile/alerts.html` + the real
`scorr_card_common.js` (for the real `fetchWithTimeout`), `fetch('/api/alerts/ideas')` intercepted
via `page.route` to return a representative fixture (1 waiting + 4 live [2 FUT-long, 1 FUT-short,
1 EQ] + 3 closed [2 FUT-long, 1 EQ] — not the founder's own live figures, a same-shape
reproduction). 24/24 checks: exactly two tabs with server counts; exactly four category chips with
client-computed, tab-scoped counts; every tab×category combination returns exactly the right
symbols; the waiting idea never appears in the DOM under any tab, ever; switching tabs resets the
category to All; every rendered ribbon reads LIVE or CLOSED, never WAITING; no
`.ia-acts`/`.ia-dist`/`.ia-sect`/`.ia-why`/`.ia-hon` element or CSS rule survives (checked against
the actual injected stylesheet with comments stripped, and the actual DOM, not the page source as
text); `#ia-foot` is a bare as-of stamp; no console errors other than this sandbox's own
unreachable Google Fonts domain (cross-checked against the actual failed-request URLs, present on
the very first load before any of this card's code runs); the bell deep-link lands on a live card
on a genuine fresh arrival.

## Not done here (the card's own FOUNDER-ONLY item)

A live open of `/m/alerts` confirming the tabs/categories against real data and that no unapproved
or waiting card appears anywhere — this container has no route to scorr.in, stated in the card
itself, not worked around.
