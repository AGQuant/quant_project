# cc#2028 — VERIFY: Alerts pure display, WOT sole approval surface, bucket membership

Read-only audit against LIVE code and real rows, not a re-read of old task results, per the card's
own instruction. Run just after cc#2027 (target/SL popup) landed at `5688e52`.

## Item 1 — Alerts pure display: APP is clean, WEB is NOT converted (real gap, reported not fixed)

**`mobile/alerts.html` (app, `/m/alerts`): CLEAN.** Zero Approve/Dismiss anywhere — confirmed by
this session's own cc#2024 rebuild (the entire Waiting rail, including `waitBody()`'s
Approve/Dismiss buttons, was deleted) and re-confirmed by a fresh grep just now.

**`trade_alerts_web.html` (web, served at `/alerts`): NOT converted — still live, not read-only.**
This page reads `/api/alerts/list?status=all&limit=200` (a different, older endpoint than the app's
`/api/alerts/ideas`) and still renders, unconditionally for pending/triggered manual alerts:

- A live `<button onclick="approve(a.id)">Approve</button>` / `<button onclick="dismiss(a.id)">
  Dismiss</button>` pair (source: lines ~278-297).
- A founder-override `<a onclick="return overrideApprove(a.id, ...)">Approve now</a>` link for a
  pending alert whose trigger has not crossed (cc#1586).
- `rebalance_due` (QB) rows get their own dedicated render (cc#1885) — a read-only "Notice" badge,
  not a button — but they are **still present on this page**, not relocated out as session_log
  40507 requires. The badge being inert does not make the row's presence here compliant.

`main.py` itself still labels the route this way, unchanged since before the 40507 reversal:
`"/alerts",  # cc#1536: Alerts (web renderer, the approve surface)`.

**Real row counts right now** (not hypothetical): `trade_alerts` currently holds **1 pending manual
alert** (`kind='entry', source_engine IS NULL`) that would render with live, clickable
Approve/Dismiss buttons on `/alerts` today, and **5 `rebalance_due` rows** (4 pending + 1 approved,
`source_engine='qb'`) still appearing on that page.

**This is a real, significant gap — not fixed here.** It is not a one-line `app_config` edit
session_log 40507/36394 already authorizes; it is a substantial UI removal on a second file this
card's own do-not-touch list did not scope in. Filed as a finding for a separate fix card, per the
card's own explicit instruction ("do not fix silently").

## Item 2 — Wall of Trades is where approval happens (confirmed, post-cc#2027)

Ran this check **after** cc#2027 landed (`5688e52`), as the card allows. `trade_wall_web.html` and
`mobile/trade_wall.html` both have live Approve/Dismiss buttons (cc#1609, pre-existing — the
"never rebuilt" framing in cc#2027's own card was a stale-comment misread, corrected in that
card's report) and, as of cc#2027, both now capture target/stop-loss before an approval completes.
The approval-window gate (cc#1760) is unchanged and correctly wired on both.

## Item 3 — `wot_buckets_enabled`, exactly the six named sources

```
app_config.wot_buckets_enabled = ["v8","index_intel","tc_scanner","qb_basket","investment_scanner","screeners"]
```

Matches `WOT_BUCKETS_DEFAULT` in `trade_wall_endpoints.py` verbatim — V8, Index Intel, TC Scanner,
QB Basket, Investment Scanner, Screeners. No `approved_alerts`, no bucket outside the founder's
named six.

**"Screeners" == the founder's "QB Scanner", confirmed both layers.** `trade_wall_endpoints.py`'s
Screeners branch reads `v13_screen_results`/`v13_screen_exits` (nightly Scorr-computed screens)
filtered to `jsonb_typeof(filters) = 'object' AND filters <> '{}'::jsonb` — third-party/FINZ-supplied
lists carry an empty filters object (their membership is supplied, not screened) and are excluded
by derivation, not by name. Checked this is the SAME exclusion at both the `(i)` info-sheet layer
(`wall_engine_rules()`) and the actual wall-data query (`_EVENTS_SQL`'s own Screeners UNION
branches) — identical clause in both places, not just documented in one and silently different in
the other.

## Item 4 — QB Basket discretionary exclusion: real keys differ from the card's own guess, verified effective

```
app_config.qb_discretionary_baskets = ["finz_defence","finz_dividend","finz_etf","finz_helios","finz_stable","finz_wcb","model_portfolio"]
```

**The card's own scope item 4 guessed different names** ("finz_helios_rotation",
"finz_wealth_compounder", "finz_vb_momentum" among them) — none of those three exist in the real
config; the real keys are `finz_helios`, `finz_wcb`, and there is no `finz_vb_momentum` entry at
all. Exactly the situation the card's own "do not assume the names" caution anticipated — reported
the real keys, not the guessed ones.

**Verified EFFECTIVE, not just configured.** `quant_paper_positions` (the table backing the QB
Basket bucket, `w.note = basket_name`) really does carry open rows for all 7 discretionary names:

| basket_name | open rows |
|---|---|
| finz_defence | 6 |
| finz_dividend | 8 |
| finz_etf | 4 |
| finz_helios | 7 |
| finz_stable | 13 |
| finz_wcb | 11 |
| model_portfolio | 20 |
| **discretionary total** | **69** |

Running the literal exclusion SQL from `trade_wall_endpoints.py` (`_QB_EXCLUDED_SQL`/
`_QB_NARROW_SQL`, copied verbatim, not reimplemented) directly against `quant_paper_positions`
returns **exactly the 6 real quant baskets and none of the 7 discretionary ones**:

| basket_name (quant, not discretionary) | open rows |
|---|---|
| alpha_multicap | 16 |
| breakout_52w | 6 |
| contra_value | 1 |
| large_cap | 16 |
| mid_cap | 12 |
| small_cap | 14 |
| **quant total, on the wall** | **65** |

69 discretionary rows correctly excluded, 65 quant-run rows correctly pass through. Zero leakage.

## Item 5 — no un-named bucket is enabled

`wot_buckets_enabled` contains exactly the six named sources and nothing else — no
`approved_alerts`, no stray bucket outside the founder's list. Nothing to flag here.

## Do-not-touch, confirmed

No code changed for this card. The one gap found (web Alerts) is a substantial UI removal, not a
config value a cited session_log already authorizes — filed as a finding, not fixed. `git status`
shows only this report added.

## Summary for a follow-up card

**Web Alerts (`trade_alerts_web.html`, `/alerts`) needs the same pure-display conversion
`mobile/alerts.html` already got this session (cc#2024)** — live Approve/Dismiss/override buttons
removed, `rebalance_due` rows relocated out, per session_log 40507. Everything else this card
checked (WOT approval surface, bucket membership, discretionary exclusion) is correct and verified
against real rows, not just configuration.
