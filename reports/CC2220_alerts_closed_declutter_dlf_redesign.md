# cc#2220 -- /m/alerts Closed tab declutter + DLF-style card redesign

## What changed

1. **Schema** (`trade_wall_approved.py`): `trade_alert_levels` gains `hidden_from_app BOOLEAN NOT
   NULL DEFAULT FALSE`, added to the CREATE TABLE DDL (fresh bootstraps) and via a new `ALTER
   TABLE ... ADD COLUMN IF NOT EXISTS` in `_ensure()` (the live table -- CREATE TABLE IF NOT
   EXISTS is a no-op there). Same idempotent in-code migration pattern
   `trade_alerts_endpoints._ensure_schema` already uses on `trade_alerts`.
2. **`resolve_close_state()`** (`trade_alerts_endpoints.py`): reads `hidden_from_app` in the same
   `trade_alert_levels` SELECT as `target_price`/`stop_loss`/`closed_at`, exposes it on the
   returned dict.
3. **`/api/alerts/ideas`**: skips appending a card when `closed AND hidden_from_app`, counted in a
   new `hidden_from_app_count` (never overloaded onto the existing `hidden_option_context`, which
   means something else -- the V10-futures-leg skip, 36703).
4. **`mobile/alerts.html`**: `cardHead()`'s `ia-live` block goes empty for a CLOSED card (the
   price/since_pct/rsLine it carried are now duplicated in the new tiles; the CLOSED ribbon is the
   one status indicator that stays). A LIVE card's `ia-live` block is byte-for-byte unchanged.
   `cardBody()`'s engine-origin plan tiles: a CLOSED card now shows ENTRY (`approved_price`) / EXIT
   (`closed.price`) / P&L (`closed.final_pct`, always; `unrealised_pnl` as a small sub-line only
   when non-null) instead of TARGET/STOP/FINAL. A LIVE card keeps TARGET/STOP/TO TARGET exactly as
   before. Every number reuses the page's existing `pctS()`/`inrS()`/`pctCls()` helpers -- no new
   formatting logic. The track bar is untouched (item 7, flagged below).

## Item 2 -- the one-time data migration could NOT run from this sandbox tonight

The MCP `run_sql` path hard-blocks `ALTER TABLE` categorically (MAINTENANCE_LOCK_RULE, cc#351) --
confirmed by trying it directly: `BLOCKED by MAINTENANCE_LOCK_RULE... 'ALTER TABLE' is a
lock-taking maintenance op`. The column can only be created by the app's own `_ensure()` running
against its own connection, which only happens once this push deploys AND a real request actually
touches `/api/alerts/ideas` or the Approved tab (both call `_ensure_levels`). That has not
necessarily happened yet as of this push, so the one-time UPSERT (mark every approved-and-closed
alert except id=25 as `hidden_from_app=TRUE`, computed from live `resolve_close_state()`, not a
hardcoded id list) is **NOT YET RUN**. It needs the column to exist first. Left as an explicit,
named follow-up rather than silently skipped or faked with a hardcoded id list.

Read directly from source (`resolve_close_state()`, `_resolve_origin`) rather than guessed: for
all 7 currently-approved alerts, a closed row's `trade_alert_levels.closed_at` is set the moment
the ideas endpoint is first read after the underlying trade closes (rule 3's engine-mirror-on-read
behaviour) -- an actively-used app almost certainly already has every genuinely-closed row's
`closed_at` populated. Once the column exists, the correct one-time statement is:

```sql
INSERT INTO trade_alert_levels (alert_id, hidden_from_app, updated_at)
SELECT a.id, TRUE, NOW()
FROM trade_alerts a JOIN trade_alert_levels l ON l.alert_id = a.id
WHERE a.status='approved' AND l.closed_at IS NOT NULL AND a.id <> 25
ON CONFLICT (alert_id) DO UPDATE SET hidden_from_app = TRUE, updated_at = NOW()
WHERE trade_alert_levels.hidden_from_app IS DISTINCT FROM TRUE;
```

This reads `closed_at IS NOT NULL` directly (rule 1's own stored fact) rather than re-deriving
`resolve_close_state()` in SQL -- valid because every row this migration targets is already
`status='approved'` and has been live on this page, so rule 3's mirror has already run for any
engine-origin close. A row still `closed_at IS NULL` at this point is genuinely open and correctly
excluded. Will run this as soon as the column is confirmed present -- watching for it on the next
touch.

## Item 4 -- default going forward (stated for founder confirmation, not asked explicitly)

A newly-closed trade defaults `hidden_from_app=FALSE` (the column default) and appears normally in
the Closed tab. This cleanup is scoped to today's existing stale/test rows only, not a standing new
default that would hide every future close. If the founder wants ongoing declutter behaviour
(e.g. auto-hide after N days), that is a separate, undecided card.

## Item 7 -- flagged, not changed

The stop/target track bar's own axis labels still read "stop" / "target" even on a redesigned
closed card whose boxes above no longer mention either word. Left exactly as-is per the card's own
instruction (the founder named the three boxes and the top block, not the track) -- flagged here
for him to decide once he sees the result, not pre-emptively reconciled.

## Validation done in this sandbox

- `ast.parse` + `import` on both changed Python modules -- clean (the expected "schema migration
  deferred to first use" warning is the same no-DATABASE_URL fallback every other card this
  session has hit, not a new error).
- `node --check` on both inline `<script>` blocks in `mobile/alerts.html` -- clean.
- `pytest tests/` -- 121 passed, 10 skipped, 0 failed (unchanged).
- Read `resolve_close_state()` / `alerts_ideas()` / `trade_wall_approved._ensure` source directly
  to confirm the wiring points and the exact column/counter names, rather than assuming them.

## What could not be verified from this sandbox

The one-time data migration (item 2, above) and the live rendered Closed tab (founder screenshot,
per the card's own verify list) -- no DATABASE_URL, no browser in this sandbox.
