# cc#2120 Part B — instrument-correct pricing: approve wiring, P&L routing, re-stamp (P1)

Builds on Part A (`reports/CC2120_partA_futures_reference_price.md`, landed `1dbab46`), which
already shipped `cmp_resolver.resolve_fut_cmp` and the popup reference-price line. Part B wires
that same resolver into the places that actually WRITE and DISPLAY money numbers.

## B1 — already built in Part A
`cmp_resolver.resolve_fut_cmp` (reused here, not rebuilt).

## B2 — approve_signal / approve_alert now instrument-aware
New `cmp_resolver.FUTURES_ENGINES = {"V8", "TC Scanner", "Index Intel"}` +
`is_futures_engine(source_engine)` — mirrors the wall's own documented ENGINE/INSTRUMENT MAP
exactly (trade_wall_endpoints.py's own header comment), not a new classification invented for
this card.

`approve_signal` (already reads `source_engine` from the POST body) and `approve_alert` (now also
`SELECT`s `source_engine` from the row) both: for a futures-engine signal, resolve via
`resolve_fut_cmp` first; if no futures bar exists right now, fall back to the existing
`resolve_cmp` (spot) — same honest-fallback shape as Part A's popup, never silently blank an
approval. For every non-futures-engine call, the code falls through to the EXACT SAME
`cmp_resolver.resolve_cmp(cur, sym)` call that existed before this diff — confirmed by reading the
diff: the fallback branch is byte-identical to the pre-existing line, not just equivalent.

**Founder ruling applied** (quoted verbatim in the card): *"Future price only check backend which
data it is serving, Approved data has its own calculation and everything."* No new column, no
ALTER TABLE — `approved_price` simply becomes the futures price for a futures-instrument signal
going forward. MAINTENANCE_LOCK_RULE (cc#351) does not apply; there is no DDL.

## B3 — Wall of Trades P&L routing, scoped to respect V8_PNL_CANON_V1 (rule 13)
Traced where the wall's P&L figures actually come from before touching anything: `e["cmp"]` /
`e["pnl"]` for every V8 open row are read straight from `v8_book_canon.open_marks()` — THE single
canonical V8 book formula, "served from ONE endpoint, consumed by EVERY surface... NO surface
recomputes book P&L or win rates locally" (rule 13). That figure is correctly **not touched** by
this card — changing it would mean the wall silently diverging from the master dashboard, trade
log tab, daylog, /m/v8 and Home, which is exactly what rule 13 forbids, and fixing the canon's own
pricing (if it needs it) is a separate, much larger card this one does not attempt.

What Part B DOES fix: `pnl_approved` — the wall's own supplementary "P&L since approval" figure,
built from `trade_alerts.approved_price`, a field the canon has no concept of at all. This was
never canon output to begin with; it is wall-specific arithmetic that happens to reuse the canon's
pure-math `unrealised_rupees()` helper. For an approved, open, futures-engine row with a real
futures bar available, the CMP fed into that one calculation is now `resolve_fut_cmp`'s value
instead of the row's spot `e["cmp"]` — so `_apx` (the entry, now futures-stamped) and the mark
come from the same instrument. Falls back to the unchanged `e["cmp"]` if no futures bar exists
right now, rather than blanking a figure that already had a mark. Diff is a clean, isolated
insertion — confirmed by reading it — touching nothing else in the render loop.

## Re-stamp — the 3 OPEN rows
OPEN/CLOSED independently re-confirmed immediately before the write (not assumed from earlier in
this session) via the real mechanism — `trade_alert_levels.closed_at IS NULL` — LEFT JOINed
against all 6 approved `kind='entry'` rows: CAMS(26)/DLF(25)/LTM(22) OPEN,
NATIONALUM(23)/ADANIGREEN(21)/TATAELXSI(15) CLOSED. Exact match to the spec's own claim, twice
now, via two different query shapes.

Re-derived the futures-close-at-approval values FRESH in this same session (not carried over from
an earlier compaction summary) via one combined `LATERAL JOIN` query — identical to the
previously-computed values, to the paisa:

| id | Symbol | Old (spot) approved_price | New (futures) approved_price | fyers_fut bar ts |
|---|---|---|---|---|
| 26 | CAMS | 706.85 | **708.60** | 2026-09-16 12:20:00 |
| 25 | DLF | 623.05 | **625.25** | 2026-09-16 12:20:00 |
| 22 | LTM | 4327.80 | **4267.20** | 2026-09-10 13:35:00 |

`UPDATE trade_alerts SET approved_price = ... WHERE id IN (26,25,22)`, keyed by `id` via a `CASE`
(no room for a WHERE-clause mismatch), `notes` stamped for auditability. Verified via `RETURNING`
on the UPDATE itself (all 3 rows, all 3 new values, exact) AND a separate follow-up `SELECT` on
the 3 CLOSED rows (23/21/15) confirming `approved_price`/`notes` are byte-unchanged — the settled
history was not touched.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on all three edited `.py` files.

**Zero diff on every protected path**: `git diff cmp_resolver.py` for this push is a pure,
isolated addition (`FUTURES_ENGINES` + `is_futures_engine`) between two untouched functions —
`SPOT_SOURCES`, `resolve_cmp`, `resolve_cmp_many` unchanged (already proven byte-identical in
Part A and not touched again here). `approve_signal`/`approve_alert`'s non-futures fallback branch
calls the exact original `resolve_cmp(cur, sym)` line. Since `tc_v4_dual.py`, `tc_v4_scan.py`,
`hr_endpoints.py`, `gvm_market_endpoints.py` and every other `resolve_cmp`/`resolve_cmp_many`
caller are files this push did not open, let alone edit (confirmed: `git status` shows only
`cmp_resolver.py`, `trade_alerts_endpoints.py`, `trade_wall_endpoints.py` changed) — their output
cannot have moved. `e["cmp"]`/`e["pnl"]` (the V8_PNL_CANON_V1-protected figures) do not appear in
this push's diff at all.

**Hand-computed P&L both ways, LTM (the open futures position with the largest gap), same-moment
prices** (fresh live read, not reused from earlier in the session — fyers_fut and fyers_eq both
at 2026-09-16 13:10:00 IST): spot cmp 4328.50, futures cmp 4329.30. Position: SHORT, qty 150
(`v8_paper_positions`, re-read fresh). `unrealised_rupees` for a SHORT = `(entry - cmp) x qty`:

| Marking | Entry (`approved_price`) | CMP | P&L |
|---|---|---|---|
| **Before this card** (spot entry, spot mark — what `pnl_approved` showed until this push) | 4327.80 | 4328.50 | **−Rs 105** |
| **The mixed-leg trap** (spot entry left alone, CMP switched to futures — what would have shipped if only B3 were built without the re-stamp) | 4327.80 | 4329.30 | **−Rs 225** |
| **After this card** (futures entry + futures mark, same instrument both sides) | 4267.20 | 4329.30 | **−Rs 9,315** |

The gap between "before" and "after" is **Rs 9,210** — the position's real futures-marked loss was
being understated by that much while both legs were priced on spot. The middle row is not a
number that ever shipped; it is included to show concretely why the card's own instruction to
re-stamp the entry IN THE SAME PUSH as the CMP switch was load-bearing, not optional — doing only
one half would have produced a THIRD, still-wrong number.

**B4 — equity/Yahoo audit, real counts**: exactly **one** approved equity-instrument alert exists
in `trade_alerts` today — id=18, HBLENGINE, `source_engine='qb'` (lowercase, `kind='rebalance_due'`,
correctly outside `FUTURES_ENGINES`, untouched by this card's pricing change). Checked its real
resolver tiers directly: zero `fyers_eq`/`fyers_ext`/`fyers_hist` 5m rows (never had a live feed);
its `cmp_prices` cache entry is **2 days 21+ hours stale** (`source='yahoo'`, last written
13-Sep), well past the 15-minute freshness window, so the cache tier currently misses; the STALE
fallback (`raw_prices`) is a 1-day-old EOD close, 720.15 from 15-Sep. Tier 3 (on-demand Yahoo) was
deliberately NOT triggered by this audit query to avoid an uncontrolled network side-effect on a
read-only check — its outcome is inherently non-deterministic at any given moment. **The real
answer: 1 of 1 approved equity alerts has no standing live-feed path today; its live-ness depends
entirely on a Yahoo on-demand call succeeding at the moment someone views it, with a 1-day-stale
close as the fallback if that call fails.** This confirms the cc#2041/2042 gap (no persistent
subscription for approved equity-only symbols) still bites — small in count (1 row) but real.

## What did NOT change
`cmp_resolver.SPOT_SOURCES`, `resolve_cmp`, `resolve_cmp_many` (byte-identical, Part A + this
push). `v8_book_canon.open_marks`, `unrealised_rupees` (used, not modified) and every OTHER
surface it feeds (master dashboard, trade log tab, daylog, /m/v8, Home) — rule 13 respected by
construction, not by promise. The 3 CLOSED `trade_alerts` rows (23/21/15) — settled history, spot
prices left exactly as recorded. `trade_alerts` schema — no ALTER, per the founder's own ruling.
The cc#2027 approve flow's two-call sequence, the approval window gate, and the Dismiss path.
cc#2041/cc#2042's equity live-subscription mechanism — not built here; B4 only measures the gap
they would have closed, per the card's own scope.
