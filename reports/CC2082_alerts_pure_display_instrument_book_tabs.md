# cc#2082 (normal) — Alerts: pure display, Instrument + Book tabs

Founder ask, verbatim: *"no point of showing pending, triggered, dismissed tab, all trades display
after approval only in approved only. instead show Equity, Future, Options, Open and Closed tabs."*

This is the follow-up cc#2028 itself recommended: session_log 40507 (ALERTS_PURE_DISPLAY_V1) ruled
Alerts becomes pure read-only display back on 07-Sep, but cc#2028's same-day audit (13-Sep) found
`trade_alerts_web.html` still had live Approve/Dismiss/override controls and QB `rebalance_due`
rows on it. This card does the pure-display conversion and the new tab structure together, one
touch of the file, per this card's own instruction not to touch it twice.

**do_not_touch respected**: `trade_wall_web.html` / `trade_wall_endpoints.py` and the Approve +
target/SL flow (cc#2027) are untouched — that surface is correct as shipped. The doctrine this
card and cc#2081/cc#2084 all rest on (WOT approves, Alerts displays) is now consolidated at
session_log 45648 WOT_ALERTS_CANON_V1.

## Item 1 — approve controls stripped, app renderer confirmed already clean

Every Approve/Dismiss/override control is removed from `trade_alerts_web.html`: the `approve()`,
`dismiss()`, `overrideApprove()` functions, the `dis`/`act` button markup, the `.al-ovr`/`.al-pend`
CSS classes' JS usage. Grep-verified after the edit — zero matches for
`approve(|dismiss(|overrideApprove|al-ovr|al-pend|al-dis` anywhere in the file.

`mobile/alerts.html` was checked, not assumed clean: grepped for the same set of markers — the
only hit is a code **comment** ("Approve/Dismiss" inside an unrelated docstring about a waiting-card
layout), zero live `approve(`/`dismiss(`/`overrideApprove`/`rebalance_due` handling anywhere.
Confirms cc#2028's finding still holds. Left untouched, per this card's own instruction.

## Item 2 — QB rebalance_due rows removed from this page

The one real `rebalance_due` row currently `status='approved'` (id 105 in my test fixture's real
shape, HBLENGINE) is dropped **client-side** in `load()`
(`.filter(function(a){ return a.kind !== 'rebalance_due'; })`) rather than by narrowing the shared
`/api/alerts/list` endpoint itself — `mobile/alerts.html` reads the same endpoint and this keeps
the change scoped to the one page actually being rebuilt.

**Where rebalance_due rows should surface instead is NOT decided by this card** (its own explicit
instruction: state this as open, do not invent a destination) — same open question cc#2028 already
left on record.

## Items 3+4 — Instrument (Equity/Future/Options) + Book (Open/Closed), two independent tab rows

Two facts approved rows never carried before, both added to `/api/alerts/list`'s payload
(`trade_alerts_endpoints.py`, approved rows only) via **`resolve_close_state()`** — the SAME cc#1781
resolver `trade_wall_approved.py`'s own Approved tab already uses for the identical open/closed
question (session_log 40507 part_2's own instruction: reuse it, don't build a second one):

- **Book** = `closed` (bool) + `closed_at` + `close_price`, straight from the resolver's own
  `closed`/`closed_at`/`close_price` fields — trade_alert_levels.closed_at, exactly as specified.
- **Instrument** = the resolver's `origin.instrument` (`FUT`/`OPT`, translated to trade_wall's own
  `FUTURES`/`OPTIONS` spelling), falling back to `EQUITY` when there's no origin (a manual alert).

**Why the resolver instead of hand-typing cc#992's engine map a second time**: the card says reuse
trade_wall_endpoints.py's own derivation, not invent a second one. `trade_wall`'s classification is
baked into its own multi-table union SQL (a different data source entirely — `trade_alerts` isn't
one of the tables in that union), so it can't be called directly; but the ONE genuinely ambiguous
case in cc#992's own map — V10/Index Intel is FUTURES or OPTIONS **per row**, from `v10_trades.leg`,
and `trade_alerts` carries no `leg` column at all — is a case `resolve_close_state`'s own origin
lookup (`_resolve_origin` → `_origin_v10`) **already resolves correctly with a real DB lookup**,
not a guess. Hand-typing a second engine-name map would still not solve that one case; reusing the
resolver does, for free, in the same call already needed for Book. The one place this reasonably
diverges from trade_wall_endpoints.py's own map: `_resolve_origin` labels "TC Scanner" as FUT where
cc#992 labels `tc_intraday_trades` EQUITY — stated here as a known, currently-inert discrepancy
(zero TC Scanner rows have ever reached `trade_alerts` — checked directly, real query, 0 rows) since
changing it would mean abandoning the resolver's correct V10 handling to chase a case with no live
data either way.

**Manual alert → EQUITY is verified, not assumed**: `scorr_alert_create.js`'s only symbol picker is
`/api/gvm/search` — a stock search (placeholder text: "Search any stock — RELIANCE, TCS, LODHA...").
A manual alert can never be created against a futures or options contract, so `origin=None` →
`EQUITY` is a checked fact, not a default of convenience.

**No "All" tab on either row** — the founder's own tab list names exactly five: Equity, Future,
Options, Open, Closed. Nothing added beyond that.

**Counts are over the whole approved book**, independent of the other row's current selection —
matches `trade_wall_endpoints.py`'s own stated convention verbatim ("Instrument/status counts are
over the WHOLE wall (both statuses / all instruments)"), not a narrower, inconsistent-with-the-
sibling-page convention invented fresh here.

## Item 5 — realised/unrealised P&L: confirmed not yet built here, not built by this card

Checked, not assumed: the pre-this-card version of `trade_alerts_web.html` had **no** P&L display
of any kind. Session_log 40507 part_2's P&L feature has not shipped on this specific page. This
card's own item 5 says *keep* whatever exists — there is nothing to keep, and building a live
Realised/Unrealised column (CMP fetching, formatting, a real second feature) was not asked for
outright. **Flagged as open, not invented**, the same way this card's own item 2 treats the
QB-placement question.

## Verify

`py_compile` clean on `trade_alerts_endpoints.py`. `node --check` clean on the extracted script
from `trade_alerts_web.html`.

**Grep-verified**: zero Approve/Dismiss/override markup or handlers remain (empty match set).
Zero `rebalance_due` render branch remains — the only surviving mentions are the removal comments
and the one active `kind !== 'rebalance_due'` filter line.

**Backend — real production Postgres, not a mock.** All 4 real approved V8 rows traced through
`resolve_close_state`'s own logic by hand against real tables: ADANIGREEN and LTM are genuinely
`OPEN` in `v8_paper_positions` (entry_ts byte-matches each alert's `source_ref`) → Book=Open;
TATAELXSI and NATIONALUM already carry a `trade_alert_levels.closed_at` → Book=Closed. All four are
`source_engine='V8'` → Instrument=Future. **Cross-checked against Wall of Trades' own numbers for
the same symbols** (this card's own verify clause): ADANIGREEN and LTM were queried directly against
`v8_paper_positions WHERE status='OPEN'` — the exact table Wall of Trades' own FUTURES union reads
— both confirmed present, so the same two real positions that will show Open on WOT show Open here
too. The one manual alert ever created (RELIANCE) is `status='dismissed'`, so it correctly never
reaches this page at all under the new `status=approved` server-side filter. **Today's real,
predicted tab picture: Future 4 (2 Open, 2 Closed); Equity 0; Options 0** — matches what the
Chromium test below proves the code does for an equivalent synthetic case.

**Frontend — 18/18 checks, real headless Chromium**, the actual page script extracted verbatim
(balance-checked) and run against a 5-row fixture covering every real shape (V8 open, V8 closed,
manual equity, an Index-Intel/options row, and a `rebalance_due` row that must never render):
zero Approve/Dismiss buttons anywhere in the live DOM; the rebalance_due row never renders;
`load()` genuinely requests `status=approved` server-side; default view (Equity+Open) shows exactly
the one real matching row; Instrument tab counts (Equity 1 / Future 2 / Options 1) and Book tab
counts (Open 3 / Closed 1) both correct over the whole set; switching to Future+Closed shows exactly
the one closed row with **both** an Approved badge and a Closed badge, the real close price
formatted correctly; switching to Options+Open surfaces the Index-Intel row, proving the V10
leg-based classification actually reaches the UI; symbol/notes stay HTML-escaped, never raw-injected
(checked with a `<img onerror=...>` fixture). One test-harness artifact caught and fixed along the
way — a shared "last fetched URL" stub variable was racing two real, independent fetch calls the
page makes (`load()` and `loadTc()`); fixed to capture every URL fetched instead of just the most
recent one.

**FOUNDER GLASS CHECK, not done here** (this container is network-blocked from scorr.in): confirm
on-glass that the Alerts page shows exactly the Future/Open pair (ADANIGREEN, LTM) and the
Future/Closed pair (TATAELXSI, NATIONALUM), with Equity and Options both empty today, matching the
real-data picture stated above.
