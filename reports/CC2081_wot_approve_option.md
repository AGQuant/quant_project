# cc#2081 (normal) — WALL OF TRADES: Approve Option button

Built under session-standing founder authorization (Fable unavailable, "clear the queue push all
to main, founder decision"). Gate re-checked before claiming: scope item 2 (5 founder decisions)
closed 14-Sep-2026 (quoted verbatim below); scope item 1 (infra discovery) was already documented
in the card's own spec. This builds scope items 3-4 — storage + surface.

## The founder's 5 answers, quoted (so a later reader trusts the source, not a summary)

- **Q1 expiry**: "SAME EXPIRY AS THE FUTURES CONTRACT... Indian single-stock options are MONTHLY
  only." Not built as a second lookup — see Design below.
- **Q2 direction**: "LONG futures signal -> BUY ATM CALL. SHORT futures signal -> BUY ATM PUT.
  Long premium only... Do NOT implement option selling/writing on this path."
- **Q3 relationship to futures approve**: "FULLY INDEPENDENT BUTTONS... A row may end up with
  futures approved only, option approved only, both, or neither... do not model this as one status
  field."
- **Q4 lot sizing**: "ALWAYS 1 LOT."
- **Q5 subscription lifecycle**: "TEAR DOWN WHEN THE OPTION TRADE IS CLOSED... a position still
  open at contract expiry is an edge case this answer does not cover — flag it."

## What is NOT built here, stated plainly before anything else

**No live options WebSocket.** The card's own discovery (already in its spec before this build
started) found none exists anywhere in this codebase — `fyers_options_feed.py` is REST polling,
index-only, and no other file carries one. Building a per-stock-option WS from scratch is new
live-trading-path infrastructure. Rule 12 requires an unambiguous, explicit CEO instruction for
that specific thing before it gets built, and this card's founder answers settle *what happens*
around a subscription (Q5) without instructing that one be built now. So `approved_price` /
`close_price` are a live, **one-shot** Fyers REST quote at the click, the same "resolver price at
the moment of approval" convention every other approve endpoint in this codebase already uses
(`trade_alerts_endpoints.py`'s `approve_alert`/`approve_signal`) — never a subscription, never
fabricated (a quote miss stores `NULL`, not a stale or invented number).

**No automatic target/SL-hit close.** Q5's teardown answer presumes a live price watch; without
one, there is nothing to detect a hit with. A manual close endpoint exists so a row is never a
one-way roach motel, but nothing closes it automatically yet.

**No display wired into the Alerts page (v8_dashboard.html's Alerts tab).** The where field's
"...show option trade in alerts" and the verify line ("the resulting entry visible on the Alerts
page") both point at the real `/alerts` surface, which resolved to a tab inside the 375KB, 11-tab
`v8_dashboard.html` (confirmed via `main.py`'s `NAV_REGISTRY`: `"/alerts": ("Alerts (web) · V8 hub
Alerts tab", ...)`), not a small standalone page. Rather than making an under-read edit to a large,
heavily-iterated file in the same push as new backend + a new WOT column, a clean, ready-to-consume
read-only endpoint is shipped instead (`GET /api/tradewall/approve_option/list`) and the actual tab
wiring is left explicitly open — a follow-up card, or a next-session pass with the file read
properly first.

## Design decisions

**Expiry resolution — reused, not re-derived.** Q1 says "same expiry as the futures contract."
Indian single-stock F&O has exactly one active contract month per name (no weeklies), so
`stock_options_backfill._resolve_strikes()`'s own "nearest listed expiry ≥ today" **is** the
futures contract's expiry by construction — confirmed by reading `_resolve_strikes` and how
`deriv_metrics.py`'s `strike_chain()` (cc#2031, this session) already uses it this same way. No
second, separate futures-expiry lookup was built; stated here so it is a checked fact, not a
silent assumption.

**ATM strike — the same real-strikes path cc#2031 already proved.** `each_side=0` on
`_resolve_strikes` returns exactly one strike: the real listed strike nearest spot, from the live
Fyers symbol master (not a guessed round-number step — stock strike steps vary by price band and
guessing one would be a fabricated strike). This is the identical function this session's cc#2031
already verified against real chain data; not re-verified here since it is unmodified, only called.

**Storage — a new sidecar table, `wot_option_trades`, not a `trade_alerts` column.** Matches the
existing `trade_alert_levels` sidecar convention (cc#1735) the card's own spec cites, and satisfies
Q3's "fully independent" instruction at the code level: this table has **no foreign key to
`trade_alerts`, keyed instead by `(source_engine, source_ref)`** — the same idempotency key
`approve_signal`/`dismiss_signal` already use — so an option approval can exist whether or not a
futures approval exists, ever exists, or is later dismissed. `CREATE TABLE`, not `ALTER TABLE`, so
outside MAINTENANCE_LOCK_RULE's gate.

**Idempotency — a partial unique index, not app-level locking.** `UNIQUE (source_engine,
source_ref) WHERE closed_at IS NULL` mirrors `trade_alerts`'s own `ON CONFLICT ... WHERE
source_engine IS NOT NULL` pattern: a second click while a row is open is a no-op (idempotent,
matches `approve_signal`'s "a double-tap is not an error"), and closing a row correctly frees the
key for a fresh approval later — verified for real, see below.

**UI — an independent column, not woven into the existing Approve/Dismiss branch.** `twCell()`
(the function rendering APPROVE/DISMISS/APPROVED/DISMISSED/suppressed states — six years... six
cards of iteration per its own comments, cc#1525→1537→1609→1736→1760→1843) is **untouched**, per
the card's own `do_not_touch`. The new `twOptCell()` is a sibling function, its own `<td>`, its
own map (`TWA.option`, mirroring `TWA.approved`), its own POST — so a bug in the new path cannot
touch the existing, well-tuned futures approve/dismiss flow, and vice versa.

## A real bug caught before it shipped

The new button reused the existing `.twa-btn` CSS class for visual consistency. `trade_wall_web.html`
has one global `click` listener keyed on that class, dispatching by `data-act` — `'approve'` and
anything else fell through to `twAct()`, which only knows `'approve'`/`'dismiss'` and would have
silently routed `'approve_option'` into its **dismiss** branch (same class, different `data-act`,
no existing case for the new value). Caught by reading the dispatcher before shipping, not after:
added an explicit `if (act === 'approve_option'){ twOptAct(id, b); return; }` branch ahead of the
fallthrough. Without this fix, clicking "APPROVE OPTION" would have called
`/api/alerts/dismiss_signal` on the real futures signal beside it.

## What changed

- **`wot_option_approve.py`** (new): `wot_option_trades` table + index; `POST
  /api/tradewall/approve_option` (the button's target — resolves ATM strike + expiry, live
  one-shot premium, inserts); `GET /api/tradewall/approve_option/map?engine=` (mirrors
  `/api/alerts/approved_map`'s shape, for the wall's paint call); `GET
  /api/tradewall/approve_option/list` (read-only listing, ready for a future Alerts-tab display);
  `POST /api/tradewall/approve_option/close` (manual only, see above). Reuses `DIRECTIONS` and
  `_approval_gate` from `trade_alerts_endpoints.py` rather than re-typing them; reuses
  `stock_options_backfill._resolve_strikes`/`strike_ticker`/`_load_token` and
  `deriv_metrics._batch_quotes` rather than re-deriving strike/quote logic a second time.
- **`main.py`**: `include_router(wot_option_approve_router)`, same pattern as every other router.
- **`trade_wall_web.html`**: new `Option` header cell (Futures instrument only); new `twOptCell()`
  / `twOptAct()` / `twLoadOptionApproved()` functions (siblings of the existing
  `twCell`/`twAct`/`twLoadApproved`, not edits to them); `TWA.option` state map; one dispatcher
  branch added ahead of the existing fallthrough (see bug above).

## What did NOT change

`trade_alerts`, `trade_alert_levels`, `trade_wall_approved.py`, `/api/alerts/approve*`,
`/api/alerts/dismiss_signal`, `twCell()`/`twAct()`/`twLoadApproved()` and every existing
APPROVE/DISMISS behavior — read, never written, by this card's new code. `futures_universe` and
the 208-symbol F&O subscription list — not read or touched at all (lot sizing is fixed at 1 by Q4,
so no lookup against it was needed for the record itself). `fyers_options_feed.py`,
`worker/fyers_feed.py` — not touched; this card's live quote path is a synchronous REST call from
the web process, not the feed worker, so the FEED WORKER DEPLOY RULE's `worker/**` gate does not
apply to this push (confirmed by checking which files actually changed, not assumed from the
card's subject matter).

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `wot_option_approve.py` and `main.py`; `node
--check` clean on `trade_wall_web.html`'s inline script (both blocks, one is an empty external-src
tag).

**A real bug, caught and fixed before shipping** — see above (the `.twa-btn` dispatcher
fallthrough).

**Real DB, not a stub — the table and its idempotency logic, run against production and cleaned
up after.** `_ensure_table`'s exact `CREATE TABLE IF NOT EXISTS` + partial-unique-index SQL run
directly against production; `information_schema.columns` and `pg_indexes` read back to confirm
both exist exactly as written (not assumed from the CREATE statement succeeding silently). Then,
against real production rows under a synthetic-but-clearly-marked key
(`source_engine='__cc2081_verify__'`): first insert succeeds; a second insert with the same
`(source_engine, source_ref)` while the first is still open is **correctly blocked** by the
partial unique index (`0` rows returned, confirmed by re-reading the table — still exactly one
row, at the first insert's values); closing that row and inserting again with the same key
**correctly succeeds** (re-approval after close is allowed, proving the `WHERE closed_at IS NULL`
partial index does what it's supposed to, not merely what the DDL claims). All synthetic rows
deleted after; `SELECT COUNT(*)` confirms zero left behind in the real table.

**Pure logic**: direction→option_type mapping (`BUY`→`CE`, `SELL`→`PE`, per Q2) checked directly.

**Not verified here, and said so rather than assumed**: the live Fyers quote round-trip itself.
This sandbox has no network egress to Fyers and no live token, so the actual `POST
/api/tradewall/approve_option` click cannot be exercised end-to-end from here — it calls
`stock_options_backfill`/`deriv_metrics` functions that are unmodified and already proved against
real chain data in cc#2031 this session, but this is the first time *this* code path calls them.
The real acceptance test is a live click on the deployed page during market hours, per the card's
own verify line and this session's established egress constraint (CC's container has no path to
scorr.in). **FOUNDER GLASS CHECK required before this card closes**, per the card's own spec.
