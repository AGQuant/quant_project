# cc#2031 (P1) — ENGINE: OPTION VALUE method fix — Black-76 forward, wing tag, sigma buckets

Built under explicit founder authorization (Fable unavailable, session-standing "clear the queue
push all to main, founder decision"). Spec source: Fable's 13-Sep-2026 audit of `option_ivp.py` +
`deriv_metrics.py` + `option_iv_daily`, superseding the cc#1994 FOUNDER_RULING_11SEP asymmetry
ruling (that ruling treated the ATM CE/PE IV gap as real market structure; this audit found it is
a pricing-input error instead — see TEST D/E below, which reproduce the audit's own cited numbers
exactly).

## Scope shipped this push: Phase A (A1-A3) + Phase B + Phase C + the A4 DRY RUN. A4's live
`option_iv_daily.iv` UPDATE is NOT run — STOPPED per the card's own instruction, awaiting founder
GO. See "A4 — STOPPED, awaiting GO" below.

## What changed

**A1 — `deriv_metrics.py`**: added `_b76_price(F,K,T,sigma,cp)` / `_b76_iv(price,F,K,T,cp)`
(Black-76, additive). Same d1/d2 machinery, same `[1e-4,5.0]`/64-iteration bisection as the
existing `_bs_price`/`_bs_iv`, which stay byte-for-byte unchanged (do_not_touch — `_price_rows`'
RV20 tag, `_options_block`'s straddle gap, and the `atm_iv_daily` snapshot all still read them
as-is). `r` is used for discounting only — no drift term in d1, since the forward already embeds
whatever carry produced it.

**A2 — `option_iv_history.py`** (the nightly bhavcopy loader): added vectorised
`_b76_price_vec`/`_b76_iv_vec` (mirrors `_bs_price_vec`/`_bs_iv_vec`'s exact structure — same
bounds/iterations — which stay defined and unchanged, just no longer called from `ingest_date()`).
Per `(symbol, trade_date)`, `ingest_date()` now computes the real put-call-parity forward
`F = K_atm + (C_atm-P_atm)*e^(R_FREE*T)` from that day's own bhavcopy closes (`K_atm` = the strike
nearest spot with BOTH legs priced), falling back to `F = spot*e^(R_FREE*T)` when no such pair
exists. `forward_fallback_count` is returned from `ingest_date()` and summed through
`run_backfill()`/`run_forward_tick()`'s own result dicts (stated in the result, per the card).

**A3 — `option_ivp.py` (`chain_tags`, the live-serving function) + its two callers in
`deriv_metrics.strike_chain()`**: `chain_tags()` gained an optional `px` param
(`{(strike,'CE'/'PE'): ltp}`). When both legs at some strike are live-quoted, pricing uses the
real parity forward from the strike nearest spot among those (`forward_source='parity'`);
otherwise it falls back to `F=spot*e^(R_FREE*T)` (`forward_source='carry_fallback'`) —
algebraically IDENTICAL to the old `_bs_price(spot,...)` call it replaces (proven in TEST A
below), so a caller with no live quotes sees the exact same numbers as before. Both real callers
(`strike_chain()`'s `option_chain`/index branch and its Fyers/stock branch) now pass `px` and
return `forward_source` at the top level of the `/api/deriv/strike-chain/{symbol}` payload.

*A genuine ordering fix found while wiring this, not assumed from the spec*: the spec states both
callers "already hold px" — true for the index branch, but the Fyers/stock branch fetched live
`ltp` via `_batch_quotes()` **after** `chain_tags` was called, and specifically **outside** the
held DB connection (a pre-existing, deliberate invariant: never hold Postgres open during a Fyers
network round-trip). Reordering `chain_tags` to run after the quotes existed would have meant
holding the connection during that round-trip; instead the branch now closes its first connection,
fetches quotes, then opens a second short connection for `chain_tags` — preserving the original
"never hold DB open during a Fyers call" invariant while giving `chain_tags` real `px`. Both
connections are read-only; nothing is written between them.

**Phase B — wing tag (`chain_tags` + `strike_fair_tag`)**: `fair_iv` (the **price**) is UNCHANGED —
still `median_atm_iv + median_skew`, exactly the original ruling. The **tag** (not the price) now
ranks the bucket's own TOTAL leg IV today (`today_atm_leg_iv + today_skew`, i.e. that bucket's own
raw stored IV) within that SAME raw leg-iv series over the window — not the skew alone. The old
skew-only percentile ignored the LEVEL entirely (e2_wing_tag evidence: an OTM leg could tag CHEAP
purely on a low skew percentile while the whole vol surface, ATM included, sat at a 95th-percentile
high). `bucket_skew_history()` now stores a 3-tuple `(trade_date, skew, raw_iv)` per entry instead
of 2 — one extra field, `raw_iv` is the same `iv` value already in hand from the row, no extra
query. ATM cell unchanged (S5 case) — verified identical below.

**Phase C — buckets (`bucket_skew_history` + `chain_tags` + `strike_fair_tag`)**: bucket key is now
`(option_type, sigma_bucket)` — **no `expiry_class`**. `option_iv_daily` carries only ONE expiry
per `(symbol, trade_date)` today (monthly), so the wk/mo split had nothing real to split on; it is
documented in code that the split MUST RETURN if weekly expiries are ever loaded. Moneyness is now
sigma-units: `z = ln(K/F) / (sigma_atm_session*sqrt(T_session))`, `bucket = round(z*2)/2`
(half-sigma steps) — one shared function, `_sigma_moneyness_bucket()`, called identically by
history (`bucket_skew_history`) and today's live chain (`chain_tags`/`strike_fair_tag`), so history
and today always classify into the same grid (ONE_REGISTRY_ONE_DERIVATION_V1). This bucket's own
forward is **always** the carry proxy `spot*e^(R_FREE*T)` — deliberately never the live parity
forward `chain_tags` prices with (`option_iv_daily` has no forward column to look a past session's
real parity forward up from, do_not_touch: no ALTER TABLE; conflating "which bucket" with "how
it's priced" would let the same word answer two drifting questions). `BUCKET_MIN_SESSIONS` stays
60, never lowered.

**Docstrings**: `option_ivp.py`'s module docstring and `option_iv_history.py`'s header both keep
their original ruled text verbatim and get a clearly-marked `AMENDED cc#2031` section explaining
what changed and why, matching this codebase's own convention (never silently rewrite a ruling).

## What did NOT change

`_bs_price`/`_bs_iv` (deriv_metrics.py) — byte-for-byte, confirmed by re-reading the file.
`_price_rows`, `_options_block`, `_stored_iv_gap_map` — untouched (do_not_touch). `option_chain_grid.py`
OI/max-pain/walls — untouched, it only consumes `strike_chain()`'s output. `pcr_mood.py`'s D-card
premium line — reads `_price_rows`' own RV20 tag/fair, not `chain_tags`' ivp fields, unaffected.
`option_iv_daily` schema — no ALTER TABLE, no new columns, no new tables. No file under `worker/**`
touched (`option_iv_history.py` is a root module, not a feed-worker file, per the do_not_touch note
itself).

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on all three files.

**Method**: every test below ran the ACTUAL edited modules (`deriv_metrics`, `option_ivp`,
`option_iv_history`, imported directly, not re-typed) against REAL `option_iv_daily` rows fetched
this session for NIFTY, BANKNIFTY, RELIANCE (250 sessions each, 2025-09-01 through 2026-09-11 —
the full table for these three symbols). The OLD `option_ivp.py` (last committed, pre-cc#2031) was
loaded from git history under a separate module name so old-vs-new could run side by side on
identical data, not a hand-reconstructed approximation of the old logic.

**TEST A — algebraic equivalence** (proves the carry-fallback path is a strict no-regression
superset, not a second diverging model): `_b76_price(F=S*e^(R_FREE*T), K, T, sigma, cp)` vs
`_bs_price(S, K, T, sigma, cp)` on 5 real (S,K,T,sigma) combinations from the fetched data —
**diffs of 1e-12 to 1e-14 (float noise), ALL_MATCH=True**.

**TEST B — round-trip** on real 2026-09-11 NIFTY rows: price → `_b76_iv` → `_b76_price` recovers
the original close to within a few paise on every sampled row.

**TEST C — vectorised == scalar**: `_b76_iv_vec`/`_b76_price_vec` (option_iv_history.py, the
nightly-loader path) vs `_b76_iv`/`_b76_price` (deriv_metrics.py, the live-chain path) on the same
42 real NIFTY rows for 2026-09-11 — max |scalar − vector| iv diff = **0.000049** (bisection
precision noise; both converge to the same answer).

**TEST D — reproduces the spec's own cited e1_forward_error evidence exactly**: recomputing the
real parity forward for NIFTY 2026-09-11 (DTE 18) from the actual fetched ATM CE/PE closes
(K_atm=23500, C=222.15, P=234.6, spot=23485.2) gives **parity F=23487.5, model F=23566.4, gap
=-78.9 pts** — an EXACT match, to the decimal, of the spec's own audited figures ("parity 23487.5
vs model 23566.4 = -78.9 pts").

**TEST E — the direct consequence, on that same session**: stored (OLD, spot-based) ATM iv:
CE=9.01%, PE=12.82%, gap=**3.81 vol pts** (the asymmetry cc#1994 called market structure). NEW
(Black-76, real parity forward) ATM iv: CE=11.01%, PE=**11.01%**, gap=**0.00 vol pts** — the two
legs converge exactly, because at the true parity forward a call and a put at the same strike are
priced consistently by construction. Confirms the wrong PREMISE finding, not a market-structure
fact.

## A4 — the destructive gate: DRY RUN result, STOPPED, awaiting founder GO

Per the card's own explicit instruction, the `option_iv_daily.iv` UPDATE has **NOT been run**. The
following is a read-only re-solve of the real, currently-stored rows for NIFTY, BANKNIFTY,
RELIANCE (9,996 rows each, 238/250 sessions with a valid ATM parity pair — the other 12 sessions
per symbol have no strike where both legs cleared close>0, all fall back to the carry forward, 0
of which needed it among these three in practice since a parity pair was always found when any
close existed).

| Symbol | ATM \|CE-PE\| gap BEFORE (median / p95) | ATM \|CE-PE\| gap AFTER (median / p95) | sessions with AFTER gap < 0.5 vol pt | floor/ceiling hits BEFORE → AFTER (of 9,996 rows) |
|---|---|---|---|---|
| NIFTY | 3.459 / 4.873 vol pts | **0.000 / 0.000** | 100.0% | 415 → 14 |
| BANKNIFTY | 3.607 / 5.054 vol pts | **0.000 / 0.000** | 100.0% | 136 → 17 |
| RELIANCE | 3.739 / 4.937 vol pts | **0.000 / 0.000** | 100.0% | 876 → 301 |

Expected per the card: "AFTER median gap < 0.005 (0.5 vol pt) on >=95% of sessions." **Actual:
100% of sessions, median exactly 0.000** on all three symbols — better than the bar, and exactly
what TEST E's mechanism predicts (parity forward makes CE/PE consistent by construction). Floor/
ceiling hit counts fall sharply on all three (a straight consequence of fewer forward-error-
distorted solves).

**The one verify bullet this dry run does NOT clear cleanly, reported exactly as found**: "no
row's iv moved from inside [0.03,1.50] to outside as a result of the re-solve." Checked per-row
(not just aggregate counts, which could hide an equal number moving each way) — **NIFTY: 4 rows,
BANKNIFTY: 2 rows, RELIANCE: 103 rows** moved from inside the band to outside.

**Root-caused, not left as a bare number**: every one of these rows (109 of 109) newly solves to
either **exactly the bisection's lower bound (0.0001)** — 108 of them — or, in one RELIANCE case,
just over the ceiling (old 1.4834, new 1.5109 — a value already sitting at the edge of the band
before the fix, tipped over by the correction). All 109 are the leg that is ITM relative to spot
(PE above spot / CE below spot), short-to-medium DTE. A floor-at-0.0001 solve means the market
close was priced **below the new, corrected intrinsic** — the classic "solver failure" case this
module's own G3 sanity gate exists to catch (its docstring's own words: "a floored or ceilinged IV
is a solver failure ... not a real observation"). The mechanism: the OLD (wrong) forward happened
to shift effective intrinsic just enough that a stale/thin bhavcopy close cleared it and looked
like a valid, if extreme, solve (old ivs on these rows ranged 9%-37%, not obviously wrong on their
own); the corrected forward exposes what the sanity gate was designed to catch all along. These
rows were already excluded from every median/percentile via `atm_iv_history`/`bucket_skew_history`'s
own `iv >= IV_FLOOR AND iv <= IV_CEILING` WHERE clause under the OLD code too, whenever they landed
outside the band — the re-solve simply moves ~109 of ~30,000 rows from "borderline-in" to
"correctly-out," concentrated overwhelmingly in RELIANCE (an individual stock, thinner/staler
option closes than the two indices — 103 of 109).

**This is presented as a finding for the founder's GO decision, not a self-granted pass.** It is
consistent with the gate doing its documented job on data that was already borderline, but it is a
real, non-zero deviation from the literal verify bullet, and the call on whether ~109 rows
(0.36% of 30,000) correctly moving to "excluded" is acceptable belongs to the founder, per the
card's own gate instruction.

## Phase C bucket coverage — an honest nuance found while trying to reproduce e3's evidence

Trying to independently reproduce the card's e3_wk_mo_starvation number precisely (rather than
just trusting it) surfaced a real subtlety worth stating plainly: the ACTUAL shipped code (old and
new alike) counts `len(series)` — **rows**, not distinct `trade_date`s — against
`BUCKET_MIN_SESSIONS`. For NIFTY specifically, on a real wk-period date tested (2026-08-24, DTE=1),
the OLD scheme's real distinct-session support in the trailing window was only 25-37 across the
ATM±10 band (would starve under a strict distinct-date reading) — but because NIFTY's 50pt strike
spacing (~0.21% of spot) packs several strikes into one whole-percent bucket, the row COUNT still
cleared 60 on every strike tested (no NO_TAG shown). RELIANCE, with wider relative strike spacing,
DID show real starvation under the code's actual row-count check: 8 of 21 ATM±10 strikes NO_TAG
on its own wk-date tested. Both symbols confirm the underlying mechanism the fix targets is real;
NIFTY's specific density happened to partially mask it under row-counting on the one date sampled.
This does not change what shipped — C1 (drop eclass) and C2 (sigma buckets) are verified correctly
implemented from the code itself (the key is genuinely `(option_type, bucket)`, confirmed by
inspection) — it is reported because asserting a clean "starvation fixed, N→M" number I could not
independently reproduce via the code's own actual counting mechanism would not be honest. One
concrete, unambiguous coverage IMPROVEMENT did show up in the real before/after run below (RELIANCE
strike 1170 CE: no tag/no fair_value under OLD → real tag+fair_value under NEW).

Finer sigma buckets are, honestly, a real trade-off: a half-sigma bucket represents a narrower
moneyness slice than a whole-percent-of-spot one, so it needs more historical sessions to fill —
visible in the same test (NIFTY strike 23700: 95 rows/35 distinct sessions under the old coarse
bucket vs 1 session under the new fine one). This is the expected cost of the e4_bucket_collapse
fix (ranking a real strike against its own moneyness peers instead of a coarse, noise-dominated
group), not a defect.

## Phase B — concrete, on real data

A real bucket (`PE`, -0.5σ, 120 sessions in window): today's skew=0.0011, today's raw iv=0.1294.
OLD tag source (skew percentile) → ivp=78.3 → EXPENSIVE. NEW tag source (raw iv percentile) →
ivp=93.3 → EXPENSIVE. Different source, can and does diverge on other cells (see the full table
below); `fair_iv` (the price) is identical either way — only what the percentile ranks against
changed, exactly as designed.

## Full before/after: NIFTY chain, 2026-09-11, ATM±10 (both legs) — the card's own REPORT ask

`forward_source=parity` (both ATM legs were quoted). **14 of 42 cells flip tag.** Fair values shift
on every single cell (Phase A repricing applies chain-wide, not just where the tag flips) — e.g.
strike 23000 CE: 640.73 → 579.61 (fair value falls ~9.5%, consistent with TEST E: the old forward
overstated calls). ATM cell (23500): tag/ivp IDENTICAL both sides (CHEAP, ivp=23.3) — confirms
"ATM cell unchanged (S5 case)" exactly; only fair_value moves (338.53 → 297.60) via the pricer
swap. Full 42-row table:

| Strike | Leg | OLD tag | OLD fair | NEW tag | NEW fair | Flip |
|---|---|---|---|---|---|---|
| 23000 | CE | EXPENSIVE | 640.73 | EXPENSIVE | 579.61 | |
| 23000 | PE | EXPENSIVE | 103.15 | FAIR | 123.59 | YES |
| 23050 | CE | EXPENSIVE | 601.70 | EXPENSIVE | 542.37 | |
| 23050 | PE | EXPENSIVE | 115.42 | FAIR | 137.57 | YES |
| 23100 | CE | EXPENSIVE | 563.80 | EXPENSIVE | 506.35 | |
| 23100 | PE | EXPENSIVE | 128.74 | FAIR | 152.67 | YES |
| 23150 | CE | EXPENSIVE | 541.19 | EXPENSIVE | 492.90 | |
| 23150 | PE | EXPENSIVE | 139.40 | EXPENSIVE | 162.96 | |
| 23200 | CE | EXPENSIVE | 506.29 | EXPENSIVE | 460.14 | |
| 23200 | PE | EXPENSIVE | 154.82 | EXPENSIVE | 180.22 | |
| 23250 | CE | EXPENSIVE | 472.61 | EXPENSIVE | 428.65 | |
| 23250 | PE | EXPENSIVE | 171.42 | EXPENSIVE | 198.71 | |
| 23300 | CE | EXPENSIVE | 440.20 | EXPENSIVE | 398.43 | |
| 23300 | PE | EXPENSIVE | 189.23 | EXPENSIVE | 218.46 | |
| 23350 | CE | EXPENSIVE | 409.08 | EXPENSIVE | 369.52 | |
| 23350 | PE | EXPENSIVE | 208.28 | EXPENSIVE | 239.49 | |
| 23400 | CE | EXPENSIVE | 393.74 | EXPENSIVE | 341.93 | |
| 23400 | PE | EXPENSIVE | 227.90 | EXPENSIVE | 261.80 | |
| 23450 | CE | EXPENSIVE | 365.49 | EXPENSIVE | 324.71 | |
| 23450 | PE | EXPENSIVE | 249.48 | EXPENSIVE | 286.88 | |
| 23500 | CE | CHEAP | 338.53 | CHEAP | 297.60 | |
| 23500 | PE | CHEAP | 272.34 | CHEAP | 310.05 | |
| 23550 | CE | EXPENSIVE | 312.86 | EXPENSIVE | 276.18 | |
| 23550 | PE | EXPENSIVE | 296.51 | EXPENSIVE | 338.00 | |
| 23600 | CE | EXPENSIVE | 288.49 | EXPENSIVE | 253.84 | |
| 23600 | PE | EXPENSIVE | 321.97 | EXPENSIVE | 365.49 | |
| 23650 | CE | FAIR | 270.55 | EXPENSIVE | 232.77 | YES |
| 23650 | PE | FAIR | 354.30 | EXPENSIVE | 394.25 | YES |
| 23700 | CE | FAIR | 248.71 | EXPENSIVE | 212.95 | YES |
| 23700 | PE | FAIR | 382.29 | EXPENSIVE | 424.26 | YES |
| 23750 | CE | FAIR | 228.12 | EXPENSIVE | 198.87 | YES |
| 23750 | PE | FAIR | 411.53 | EXPENSIVE | 469.10 | YES |
| 23800 | CE | FAIR | 208.76 | EXPENSIVE | 181.37 | YES |
| 23800 | PE | FAIR | 441.98 | EXPENSIVE | 501.25 | YES |
| 23850 | CE | FAIR | 194.58 | EXPENSIVE | 165.03 | YES |
| 23850 | PE | EXPENSIVE | 487.18 | EXPENSIVE | 534.52 | |
| 23900 | CE | FAIR | 177.50 | EXPENSIVE | 149.80 | YES |
| 23900 | PE | EXPENSIVE | 519.70 | EXPENSIVE | 568.88 | |
| 23950 | CE | FAIR | 161.54 | EXPENSIVE | 135.66 | YES |
| 23950 | PE | EXPENSIVE | 553.32 | EXPENSIVE | 604.31 | |
| 24000 | CE | FAIR | 146.68 | FAIR | 125.96 | |
| 24000 | PE | EXPENSIVE | 588.01 | EXPENSIVE | 659.47 | |

## RELIANCE, same date, via the Fyers/stock path simulation

`forward_source=parity`. **32 of 42 cells flip.** A genuine coverage improvement, not just a
repricing: strike 1170 CE had **no tag, no fair_value** under OLD (bucket starved) — NEW gives
**CHEAP, 98.76** (real bucket coverage, sigma-bucketed). `px=None` (no live quotes) correctly
returns `forward_source='carry_fallback'`, confirmed by an explicit call with `px` omitted.

## Not built this push (explicitly out of scope per the card)

The option chain popup UI ('i' button + moving the dot legend) — separate card, filed after this
lands, founder request 13-Sep. Weekly-expiry loading. Window length (120 vs 252). Dividend-yield
modelling (the parity forward absorbs it).
